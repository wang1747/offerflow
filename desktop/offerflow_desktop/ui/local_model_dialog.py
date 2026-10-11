"""本地模型配置：给「本地知识库」的图谱抽取 / 记忆提炼填一份**本机直连**的模型。

为什么不复用「模型设置」那一页：

  · 「模型设置」保存的是**云端账号**配置（`/api/v1/user/model-config/`），
    Key 由服务端加密、只回掩码 —— 本机拿不到明文，也就无法直连模型；
  · 「本地知识库」的定位是「离线可用、资料不出本机」，最合适的搭配恰恰是
    **本机 Ollama**（`http://127.0.0.1:11434/v1`），而云端那条保存路径
    因为防 SSRF 会拒绝 localhost / 内网地址。

所以这里单独存一份（`local_model.*`），并且 **API Key 允许留空**。

保存位置：本地 SQLite 的 `local_model.*`，与云端那份互不影响。
在「模型设置」里保存一次，也会把同一份配置同步写到这里（双写）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout,
)

from .. import database as local_db
from ..llm import LLMError, LLMWorker, test_connection
from .components import api_guard, hint_label, primary_button, set_result_message

_URL_PREFIXES = ("http://", "https://")


def _probe(base_url: str, api_key: str, model: str) -> str:
    """测试连接，把底层异常翻成人话（这里不走 `api_guard` —— 那是给云端接口用的）。"""
    import requests

    try:
        return test_connection(base_url, api_key, model)
    except requests.exceptions.ConnectionError as exc:
        raise LLMError(f"连不上 {base_url}（本机模型没启动？）：{exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise LLMError("连接超时，模型可能正在加载，稍后重试") from exc
    except requests.exceptions.HTTPError as exc:
        resp = getattr(exc, "response", None)
        code = resp.status_code if resp is not None else "?"
        raise LLMError(f"模型服务返回错误（{code}）") from exc
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"测试失败：{type(exc).__name__}: {exc}") from exc


class LocalModelDialog(QDialog):
    """本机模型配置对话框（Base URL / 模型名 / 可选 Key）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: Optional[LLMWorker] = None
        self._busy = False
        self.setWindowTitle("本地模型（本地知识库）")
        self.setModal(True)
        self.setMinimumWidth(620)
        self._build()
        self._load()

    # ==================== 界面 ====================

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        title = QLabel("给本地知识库单独配一个模型")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        lay.addWidget(title)
        lay.addWidget(hint_label(
            "「抽取图谱 / 提炼记忆」在本机直连模型（资料不出本机、断网可用），"
            "需要本机能拿到明文 Key —— 云端那份只回掩码，拿不回来。\n"
            "想完全离线就用本机 Ollama：Base URL 填 http://127.0.0.1:11434/v1，"
            "模型名填你 ollama list 里的名字（如 qwen2.5:7b），API Key 留空即可。\n"
            "在「模型设置」里保存一次，也会把同一份配置同步到这里。"
        ))

        form = QFormLayout()
        form.setSpacing(10)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("http://127.0.0.1:11434/v1")
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("qwen2.5:7b")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("本机 Ollama 可留空；云端模型才需要")
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("模型名", self.model_edit)
        form.addRow("API Key", self.key_edit)
        lay.addLayout(form)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.hide()
        lay.addWidget(self.result_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.clicked.connect(self._on_test)
        self.save_btn = primary_button("保存")
        self.save_btn.clicked.connect(self._on_save)
        self.clear_btn = QPushButton("清除")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self._on_clear)
        self.close_btn = QPushButton("取消")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.test_btn)
        row.addWidget(self.save_btn)
        row.addWidget(self.clear_btn)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

    # ==================== 数据 ====================

    def _load(self) -> None:
        try:
            cfg = local_db.get_local_model_config()
        except Exception:  # noqa: BLE001
            cfg = {}
        self.base_url_edit.setText(cfg.get("base_url") or "")
        self.model_edit.setText(cfg.get("model") or "")
        if cfg.get("has_config"):
            set_result_message(
                self.result_label, True,
                f"当前本机模型：{cfg.get('model')}"
                f"（{cfg.get('base_url')}）"
                f"{'　Key：' + cfg['api_key_masked'] if cfg.get('api_key_masked') else '　无 Key'}",
            )

    def _collect(self) -> Optional[Dict[str, str]]:
        base_url = self.base_url_edit.text().strip()
        model = self.model_edit.text().strip()
        api_key = self.key_edit.text().strip()
        if not base_url or not base_url.lower().startswith(_URL_PREFIXES):
            set_result_message(self.result_label, False,
                               "Base URL 需要以 http:// 或 https:// 开头。")
            return None
        if not model:
            set_result_message(self.result_label, False, "请填写模型名。")
            return None
        return {"provider": "local", "base_url": base_url,
                "model": model, "api_key": api_key}

    # ==================== 操作 ====================

    def _set_busy(self, busy: bool, action: str = "") -> None:
        self._busy = busy
        for btn in (self.test_btn, self.save_btn, self.clear_btn, self.close_btn):
            btn.setEnabled(not busy)
        if busy:
            if action == "test":
                self.test_btn.setText("测试中…")
            elif action == "save":
                self.save_btn.setText("保存中…")
        else:
            self.test_btn.setText("测试连接")
            self.save_btn.setText("保存")

    def _run(self, action: str, fn, ok_cb) -> None:
        self._set_busy(True, action)
        self.result_label.hide()
        worker = LLMWorker(api_guard(fn))
        worker.success.connect(lambda payload: self._on_done(True, payload, ok_cb))
        worker.failed.connect(lambda msg: self._on_done(False, msg, ok_cb))
        self._worker = worker
        worker.start()

    def _on_done(self, ok: bool, payload: Any, ok_cb) -> None:
        self._set_busy(False)
        if ok:
            ok_cb(payload)
        else:
            set_result_message(self.result_label, False, str(payload))

    def _on_test(self) -> None:
        fields = self._collect()
        if fields is None:
            return
        self._run(
            "test",
            lambda: _probe(fields["base_url"], fields["api_key"], fields["model"]),
            lambda msg: set_result_message(self.result_label, True, f"连接成功：{msg}"),
        )

    def _on_save(self) -> None:
        fields = self._collect()
        if fields is None:
            return

        def do():
            local_db.save_local_model_config(
                fields["provider"], fields["base_url"], fields["model"], fields["api_key"]
            )
            return local_db.get_local_model_config()

        self._run("save", do, self._on_saved)

    def _on_saved(self, cfg: Dict[str, Any]) -> None:
        self._load()
        set_result_message(
            self.result_label, True,
            f"已保存：{cfg.get('model')}（{cfg.get('base_url')}）。"
            "本地知识库的「抽取图谱 / 提炼记忆」现在可用。",
        )
        self.accept()

    def _on_clear(self) -> None:
        ans = QMessageBox.question(
            self, "清除本地模型",
            "确定清除本机的模型配置吗？\n\n"
            "清除后「抽取图谱 / 提炼记忆」会回到未配置状态（云端那份不受影响）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            local_db.clear_local_model_config()
        except Exception as exc:  # noqa: BLE001
            set_result_message(self.result_label, False, f"清除失败：{type(exc).__name__}: {exc}")
            return
        self.base_url_edit.clear()
        self.model_edit.clear()
        self.key_edit.clear()
        set_result_message(self.result_label, True, "本机模型配置已清除。")
