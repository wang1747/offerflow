"""职位要求分析页：粘贴 JD → 结构化解析 → JD 库管理。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QAbstractItemView,
)

from ... import api_client as db
from ...llm import LLMWorker
from ...core.ocr import ocr_image_file, is_ocr_available
from ..components import (BusyOverlay, card, danger_button, empty_state, flow_tags,
                          hint_label, make_title, primary_button, section_label)


class JDAnalysisPage(QWidget):
    def __init__(self, main_window, with_title: bool = True):
        super().__init__()
        self.mw = main_window
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        if with_title:
            layout.addWidget(make_title(
                "职位要求分析",
                "粘贴职位描述原文，AI 自动提取公司、职位、硬性要求、加分项、技术栈与隐藏信号。",
            ))

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ===== 左：输入 + 解析结果 =====
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)

        input_card = card()
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(16, 14, 16, 14)
        input_layout.addWidget(section_label("JD 原文"))
        self.jd_input = QPlainTextEdit()
        self.jd_input.setPlaceholderText("粘贴职位描述全文（职责 + 任职要求 + 加分项）…")
        self.jd_input.setFixedHeight(180)
        input_layout.addWidget(self.jd_input)
        self.parse_btn = primary_button("解析 JD")
        self.parse_btn.clicked.connect(self._parse)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.parse_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.ocr_btn = QPushButton("截图识别 JD")
        self.ocr_btn.setToolTip("选择一张 JD 截图，用系统 OCR 自动识别文字填入上方输入框")
        self.ocr_btn.clicked.connect(self._ocr_import)
        btn_row.addWidget(self.ocr_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.file_btn = QPushButton("从文件导入")
        self.file_btn.setToolTip("选择 PDF / Word / 文本文件，本地解析后填入上方输入框（不联网）")
        self.file_btn.clicked.connect(self._file_import)
        btn_row.addWidget(self.file_btn, 0, Qt.AlignmentFlag.AlignLeft)
        btn_row.addStretch(1)
        input_layout.addLayout(btn_row)
        left_layout.addWidget(input_card)

        self.result_card = card()
        self.result_layout = QVBoxLayout(self.result_card)
        self.result_layout.setContentsMargins(16, 14, 16, 14)
        self.result_host = QWidget()
        self.result_inner = QVBoxLayout(self.result_host)
        self.result_inner.setContentsMargins(0, 0, 0, 0)
        self.result_scroll = QScrollArea()
        self.result_scroll.setWidgetResizable(True)
        self.result_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.result_scroll.setWidget(self.result_host)
        self.result_layout.addWidget(self.result_scroll)
        left_layout.addWidget(self.result_card, 1)
        self.result_inner.addWidget(empty_state(
            "还没有解析结果",
            "粘贴 JD 原文后点「解析 JD」，公司、硬性要求、技术栈会显示在这里"))

        splitter.addWidget(left)

        # ===== 右：JD 库 =====
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.addWidget(section_label("我的岗位库"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["公司", "职位", "创建时间", "操作"])
        hdr = self.table.horizontalHeader()
        # 公司/职位：拉伸占满；创建时间/操作：固定宽度避免文字被挤断。
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(2, 96)
        hdr.resizeSection(3, 72)
        # 允许双击「公司 / 职位」单元格直接改名（创建时间 / 操作 列保持只读）。
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellChanged.connect(self._on_cell_edited)
        right_layout.addWidget(self.table)
        row = QHBoxLayout()
        view_btn = QPushButton("查看详情")
        view_btn.clicked.connect(self._view_detail)
        del_btn = danger_button("删除选中")
        del_btn.clicked.connect(self._delete_selected)
        row.addWidget(view_btn)
        row.addWidget(del_btn)
        agent_btn = QPushButton("用 Agent 制定行动")
        agent_btn.clicked.connect(self._open_agent_for_jd)
        row.addWidget(agent_btn)
        row.addStretch(1)
        right_layout.addLayout(row)
        right_layout.addWidget(hint_label("提示：解析后的岗位会出现在「岗位分析 · 匹配度」「简历优化」「投递追踪」的候选列表中。"))
        splitter.addWidget(right)
        splitter.setSizes([600, 460])

        layout.addWidget(splitter, 1)
        self.overlay = BusyOverlay(self)
        self._table_loading = False
        self._refresh_table()

    def _refresh_table(self) -> None:
        # 防止「程序填充单元格」触发 cellChanged → 递归 self._refresh_table。
        self._table_loading = True
        try:
            jds = db.list_jds()
            self.table.setRowCount(len(jds))
            for i, jd in enumerate(jds):
                c0 = QTableWidgetItem((jd.get("company") or "").strip())
                c1 = QTableWidgetItem((jd.get("position") or "").strip())
                c2 = QTableWidgetItem((jd.get("created_at") or "")[:10])
                c3 = QTableWidgetItem("右键 ▸")
                c3.setToolTip("右键该行可「查看详情 / 删除」")
                # 仅「公司 / 职位」两列可双击编辑；创建时间、操作列保持只读。
                c0.setFlags(c0.flags() | Qt.ItemIsEditable)
                c1.setFlags(c1.flags() | Qt.ItemIsEditable)
                c2.setFlags(c2.flags() & ~Qt.ItemIsEditable)
                c3.setFlags(c3.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(i, 0, c0)
                self.table.setItem(i, 1, c1)
                self.table.setItem(i, 2, c2)
                self.table.setItem(i, 3, c3)
        finally:
            self._table_loading = False
        self.mw.refresh_status()

    def _on_cell_edited(self, row: int, col: int) -> None:
        """双击改完「公司 / 职位」后落库（问题修复：此前 JD 不能改名）。"""
        if getattr(self, "_table_loading", False):
            return
        if col not in (0, 1):
            return
        jds = db.list_jds()
        if row < 0 or row >= len(jds):
            return
        jd = jds[row]
        item = self.table.item(row, col)
        new_text = (item.text() if item else "").strip()
        field = "company" if col == 0 else "position"
        old_text = (jd.get(field) or "").strip()
        if new_text == old_text:
            return
        try:
            db.update_jd(jd["id"], **{field: new_text})
        except db.ApiError as exc:
            QMessageBox.warning(self, "改名失败", str(exc))
            self._refresh_table()   # 还原单元格
            return
        # 改名成功：刷新让表格与右下提示一致（如面试/优化页的下拉也会读到新名）。
        self._refresh_table()

    def _on_context_menu(self, pos) -> None:
        """右键行的真实菜单：查看详情 / 删除选中（此前「操作」列写的右键是假的）。"""
        jd = self._selected_jd()
        if not jd:
            return
        menu = QMenu(self)
        view_act = menu.addAction("查看详情")
        del_act = menu.addAction("删除选中")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action is view_act:
            self._show_result(jd)
        elif action is del_act:
            self._delete_selected()

    def _open_agent_for_jd(self) -> None:
        """从 JD 库带着当前岗位进入求职 Agent（审计 P1-2）。"""
        jd = self._selected_jd()
        if not jd:
            QMessageBox.information(self, "提示", "请先在右侧列表选择一条岗位")
            return
        self.mw.open_agent({"jd_id": jd["id"], "entry_source": "jd_analysis"})

    def _parse(self) -> None:
        raw_text = self.jd_input.toPlainText().strip()
        if not raw_text:
            QMessageBox.warning(self, "缺少内容", "请先粘贴 JD 原文")
            return
        self.parse_btn.setEnabled(False)
        self.overlay.show_over("AI 正在解析职位要求…")
        self._worker = LLMWorker(lambda: db.save_jd(raw_text, None))
        self._worker.success.connect(self._on_parsed)
        self._worker.failed.connect(self._on_parse_failed)
        self._worker.start()

    def _file_import(self) -> None:
        """选择本地 JD 文件（PDF / Word / 文本），本地解析后填入输入框。

        解析走 local_ai.parse（P1）：PDF 用 LiteParse，扫描件自动转系统 OCR，
        DOCX 用 python-docx；全程不联网、不消耗模型额度。
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 JD 文件", "",
            "JD 文件 (*.pdf *.docx *.txt *.md);;所有文件 (*)")
        if not path:
            return
        self.file_btn.setEnabled(False)
        self.overlay.show_over("正在本地解析文件…")

        def run():
            from ...local_ai import parse_document

            return parse_document(path)

        self._worker = LLMWorker(run)
        self._worker.success.connect(self._on_file_done)
        self._worker.failed.connect(self._on_file_failed)
        self._worker.start()

    def _on_file_done(self, result) -> None:
        self.overlay.hide()
        self.file_btn.setEnabled(True)
        text = (getattr(result, "text", "") or "").strip()
        if not text:
            QMessageBox.warning(
                self, "解析失败",
                getattr(result, "note", "") or "未能从该文件提取到文字，请手动粘贴 JD 原文")
            return
        self.jd_input.setPlainText(text)
        engine = getattr(result, "engine", "") or "本地解析"
        msg = f"已解析（{engine}），文字已填入输入框，请核对后点「解析 JD」。"
        note = getattr(result, "note", "")
        if note:
            msg += f"\n\n{note}"
        QMessageBox.information(self, "解析完成", msg)

    def _on_file_failed(self, msg: str) -> None:
        self.overlay.hide()
        self.file_btn.setEnabled(True)
        QMessageBox.warning(self, "解析失败", msg)

    def _ocr_import(self) -> None:
        """选择一张 JD 截图，用系统 OCR 识别后填入输入框。"""
        if not is_ocr_available():
            QMessageBox.warning(
                self, "OCR 不可用",
                "本机未启用 Windows OCR 引擎。\n"
                "请直接截图后手动复制文字粘贴到输入框。",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 JD 截图", "", "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        self.ocr_btn.setEnabled(False)
        self.overlay.show_over("正在识别截图文字…")

        def run():
            return ocr_image_file(path)

        self._worker = LLMWorker(run)
        self._worker.success.connect(self._on_ocr_done)
        self._worker.failed.connect(lambda msg: self._on_ocr_failed(msg))
        self._worker.start()

    def _on_ocr_done(self, text: str) -> None:
        self.overlay.hide()
        self.ocr_btn.setEnabled(True)
        if not text:
            QMessageBox.warning(self, "识别失败", "未能从截图中识别出文字，请换一张更清晰的图片或手动粘贴")
            return
        self.jd_input.setPlainText(text)
        QMessageBox.information(self, "识别完成", "已识别截图文字并填入输入框，请核对后点「解析 JD」。")

    def _on_ocr_failed(self, msg: str) -> None:
        self.overlay.hide()
        self.ocr_btn.setEnabled(True)
        QMessageBox.warning(self, "识别失败", msg)

    def _on_parse_failed(self, msg: str) -> None:
        self.overlay.hide()
        self.parse_btn.setEnabled(True)
        QMessageBox.warning(self, "解析失败", msg)

    def _on_parsed(self, parsed: dict) -> None:
        self.overlay.hide()
        self.parse_btn.setEnabled(True)
        # save_jd 已解析并保存，parsed 即完整 jd dict（含 id）
        self._show_result(parsed)
        self._refresh_table()
        if parsed.get("parsed_pending"):
            # 离线时服务端的 LLM 解析做不了：如实告知，不假装解析成功
            QMessageBox.information(
                self, "已保存（当前离线）",
                "JD 原文已存在本地，联网后会自动解析出硬性要求/技术栈，并同步到服务器。")

    def _show_result(self, jd: dict) -> None:
        while self.result_inner.count():
            item = self.result_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        title = QLabel(f"{jd.get('company') or '未知公司'} · {jd.get('position') or '未知职位'}")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        self.result_inner.addWidget(title)

        if jd.get("must_have"):
            row = card(danger=True)
            rl = QVBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            rl.addWidget(hint_label("硬性要求（must have）"))
            rl.addWidget(flow_tags(jd["must_have"], "#dc2626"))
            self.result_inner.addWidget(row)
        if jd.get("nice_to_have"):
            row = card(warn=True)
            rl = QVBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            rl.addWidget(hint_label("加分项（nice to have）"))
            rl.addWidget(flow_tags(jd["nice_to_have"], "#d97706"))
            self.result_inner.addWidget(row)
        ts = jd.get("tech_stack") or {}
        ts_items = [v for values in ts.values() for v in values] if isinstance(ts, dict) else []
        if ts_items:
            row = card(highlight=True)
            rl = QVBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            rl.addWidget(hint_label("技术栈"))
            rl.addWidget(flow_tags(ts_items, "#1d4ed8"))
            self.result_inner.addWidget(row)
        if jd.get("hidden_signals"):
            row = card()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            rl.addWidget(hint_label("隐藏信号（JD 里没明说但值得注意的）"))
            for s in jd["hidden_signals"]:
                label = QLabel("• " + str(s))
                label.setWordWrap(True)
                rl.addWidget(label)
            self.result_inner.addWidget(row)
        self.result_inner.addStretch(1)

    def _selected_jd(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        jds = db.list_jds()
        return jds[row] if row < len(jds) else None

    def _view_detail(self) -> None:
        jd = self._selected_jd()
        if jd:
            self._show_result(jd)

    def _delete_selected(self) -> None:
        jd = self._selected_jd()
        if not jd:
            QMessageBox.information(self, "提示", "请先在右侧列表选择一条岗位")
            return
        confirm = QMessageBox.question(
            self, "删除确认",
            f"确定删除「{jd.get('company')} · {jd.get('position')}」？\n关联的匹配结果会一并删除，投递记录将解除关联。",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            # 删除失败必须让用户知道：P4 之后离线删除是「本地先删 + 排队上行」，
            # 所以不再拒绝用户，但必须说清「还没同步到服务器」。
            try:
                db.delete_jd(jd["id"])
            except db.ApiOfflineError:
                QMessageBox.information(
                    self, "已离线删除",
                    "网络不可用：这条岗位已从本地移除，联网后会自动同步删除。")
            except db.ApiError as exc:
                QMessageBox.warning(self, "删除失败", str(exc))
                return
            self._refresh_table()
