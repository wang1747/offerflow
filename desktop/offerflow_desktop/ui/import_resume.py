"""简历导入：共享的「导入已有简历」对话框 + 「导入后自动选中」流程。

为什么放在 `ui/` 顶层而不是 `ui/pages/`：
「导入简历」不再只是「简历生成」页的功能 —— 面试准备 / 薪资谈判 / 岗位匹配
都要支持「没有简历时就地补一份」，否则用户光想用其中一两个模块，还得先跑去
别处准备简历。多个页面复用同一份实现，放在 `ui/` 顶层可以让 `ui/pages/*`
单向依赖，不产生循环导入（`ui/pages/resume_generate.py` 也从这里取对话框）。

解析在本地完成（`local_ai.parse`：PDF → LiteParse / DOCX → python-docx），
只把**提取出的文本**上行入库，原文件不出本机。

`.json` 走的是另一条路（JSON Resume，见 `core/json_resume.py`），但终点一样：
先把结构化数据渲染成一段正文，再走同一个 `upload_resume()` —— 因为入库接口
只接受 `raw_text`，服务端会自己再解析一遍。所以「导入 JSON」并不比导入 Word 特殊。
"""

from __future__ import annotations

import os
from typing import Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .. import api_client as db
from ..core import json_resume
from ..llm import LLMWorker
from .components import hint_label, primary_button


def extract_resume_text(path: str) -> str:
    """从 .docx / .pdf / .txt 提取简历文本，失败抛 ValueError。

    解析实现在 `local_ai.parse`（P1，全本地、不联网）：
        PDF   → LiteParse（自带 PDFium、免模型；扫描件自动转 Windows 系统 OCR）
        DOCX  → python-docx
        TXT   → 编码探测直读
    本函数只保留统一入口与「失败抛 ValueError」的异常语义，供 UI 直接捕获展示。
    """
    from ..local_ai import extract_text

    return extract_text(path)


def extract_any(path: str) -> Tuple[str, str]:
    """按扩展名分派解析，返回 ``(简历正文, 附注)``；失败抛 ValueError。

    附注是给界面看的一句提示（例如"这份 JSON 没有 basics 段"），
    没有话要说时是空串。
    """
    if os.path.splitext(path)[1].lower() != ".json":
        return extract_resume_text(path), ""

    # 二进制读 + 自己解码：Windows 的文本模式会按本地编码猜，
    # 而 JSON Resume 规范要求 UTF-8；utf-8-sig 顺带吃掉编辑器可能写进去的 BOM。
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("这个 JSON 文件不是 UTF-8 编码。"
                         "JSON Resume 要求 UTF-8，请另存为 UTF-8 后重试") from exc
    data = json_resume.loads(text)                 # 解析失败会抛带行号的 ValueError
    looks_like, note = json_resume.probe(data)
    if not looks_like:
        raise ValueError(note)
    rendered = json_resume.to_plain_text(json_resume.from_json_resume(data))
    if not rendered.strip():
        raise ValueError("这份 JSON 里没有可用的简历内容")
    return rendered, note


class ImportResumeDialog(QDialog):
    """选择 Word/PDF 文件导入已有简历，直接入库（无需重新生成）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入已有简历")
        self.resize(560, 440)
        self.payload = None                      # 收起后交给调用方上传（dict）
        self._text = ""
        self._filename = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # 选择文件（大按钮）
        self.file_btn = QPushButton("选择 Word / PDF / JSON 简历文件")
        self.file_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.file_btn.setMinimumHeight(48)
        self.file_btn.clicked.connect(self._pick_file)
        layout.addWidget(self.file_btn)

        self.file_label = hint_label("未选择文件（支持 .docx / .pdf / .json）")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        # 提取内容预览（只读）
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择文件后，这里显示提取到的简历内容预览…")
        layout.addWidget(self.preview, 1)

        layout.addWidget(hint_label(
            "导入后即可用于「岗位分析 · 匹配度」「简历优化」「面试准备」「薪资谈判」；"
            "姓名、求职意向等由云端从简历正文自动提取。"))

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.ok_btn = primary_button("导入")
        self.ok_btn.setEnabled(False)
        self.ok_btn.clicked.connect(self._import)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self.ok_btn)
        layout.addLayout(btn_row)

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择简历文件", "",
            "简历文件 (*.docx *.pdf *.json);;Word (*.docx);;PDF (*.pdf);;"
            "JSON Resume (*.json)")
        if not path:
            return
        try:
            text, note = extract_any(path)
            if not text.strip():
                QMessageBox.warning(self, "无法读取", "未能从该文件提取到文本（可能是扫描版 PDF）。")
                return
            self._text = text.strip()
            self._filename = os.path.basename(path)
            self.preview.setPlainText(self._text)
            self.file_label.setText(
                f"已选择：{self._filename}" + (f"（{note}）" if note else ""))
            self.ok_btn.setEnabled(True)
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", str(exc))

    def _import(self):
        """只做校验并收起对话框；上传交给调用方异步执行。

        坑：早期版本在这里直接 `db.save_resume(...)`（旧的本地 SQLite 写法），
        迁到云端后 `db` 已指向 api_client，该方法不存在 → 点「导入」必抛
        AttributeError，简历既进不了库、用户也看不到任何提示。
        模态框里也不要发请求：一旦响应返回晚于对话框销毁，回调会摸到已释放的
        C++ 对象（Qt abort、进程静默消失）。所以这里只收集数据。
        """
        if len(self._text) < 30:
            QMessageBox.warning(self, "内容太短", "提取到的简历内容太短，请确认文件是否完整")
            return
        self.payload = {"raw_text": self._text, "source_file": self._filename}
        self.accept()


def resume_empty_hint(text: str) -> QLabel:
    """「还没有简历」引导条：默认隐藏，由页面的刷新逻辑按需显隐。"""
    label = hint_label(text)
    label.setVisible(False)
    return label


def make_import_button(page: QWidget, on_imported, tooltip: str = "") -> QPushButton:
    """做一个「导入简历」按钮，点击即走完整的导入 → 自动选中流程。

    `page` 传页面自身（需要它的 `mw.refresh_status()`）。
    `on_imported(resume_id)` 由调用方实现：刷新自己的简历下拉并选中该 id。
    """
    btn = QPushButton("导入简历")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setToolTip(tooltip or "从 Word / PDF 文件导入一份已有简历（本机解析，不上传原文件）")
    btn.clicked.connect(lambda: import_resume_and_select(page, on_imported, button=btn))
    return btn


def import_resume_and_select(page, on_imported, button: QPushButton | None = None) -> bool:
    """弹「导入已有简历」→ 本地解析 → 入库 → 回调 `on_imported(新简历 id)`。

    返回 False 表示用户取消（未发起上传）。

    时序要点：**必须等入库成功之后**才回调刷新，否则下拉里还没有这一条，
    会出现「值是新的、选项里没有」的空档（网页端同一个坑）。
    """
    dlg = ImportResumeDialog(page)
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.payload:
        return False

    payload = dlg.payload
    if button is not None:
        button.setEnabled(False)
    # 不占用页面的 self._worker：那个槽位可能正跑着本页的生成任务。
    worker = LLMWorker(
        lambda: db.upload_resume(payload["raw_text"], payload["source_file"]))

    def _re_enable() -> None:
        if button is not None:
            button.setEnabled(True)

    def _on_done(saved) -> None:
        # 恢复按钮必须写在这里，**不能**挂在 worker.finished 上：
        # LLMWorker 的保命引用 `_ALIVE` 正是在 finished 时被它自己 `discard` 的，
        # 而本函数没有别的地方持有 worker（页面用的是 self._worker 槽位，不共用）
        # → `finished` 一发出，worker 的 Python 包装就可能被回收，后续排队的槽
        # 被静默丢掉，按钮永远停在禁用态。实测：接 finished 必失败，写在 success/
        # failed 里 100% 生效。
        _re_enable()
        saved = saved if isinstance(saved, dict) else {}
        try:
            page.mw.refresh_status()
        except Exception:
            pass
        ver = saved.get("version")
        QMessageBox.information(
            page, "导入成功",
            "简历已导入%s，已自动选中，可直接使用。"
            % (f"（版本 v{ver}）" if ver else ""))
        new_id = saved.get("id")
        if new_id and callable(on_imported):
            on_imported(new_id)

    def _on_failed(msg: str) -> None:
        _re_enable()
        QMessageBox.warning(page, "导入失败", str(msg))

    worker.success.connect(_on_done)
    worker.failed.connect(_on_failed)
    worker.start()
    return True
