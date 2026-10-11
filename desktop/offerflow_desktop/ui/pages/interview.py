"""面试准备页：面经库 / 面试题生成 / 模拟面试 三个标签。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
    QAbstractItemView, QInputDialog, QScrollArea, QGridLayout,
)
from shiboken6 import isValid

from ... import api_client as db
from ...llm import LLMWorker
from ..components import (
    BusyOverlay, api_guard, card, danger_button, empty_state, flow_tags, hint_label,
    make_title, primary_button, section_label, tag_label,
)
from ..import_resume import make_import_button, resume_empty_hint

# 面经导入支持的文件类型。
#
# 与网页版不同：网页版把原文件 multipart 传给服务端解析；桌面版**在本机解析**
# （`local_ai.parse`：PDF/DOCX/TXT/MD + 图片走 Windows 系统 OCR），只把抽出来的
# 文字入库 —— 原文件不出本机。这也是本项目「本地优先」的既定取向。
# 只列**真能解析**的扩展名：`parse_document` 不支持 .html，列进筛选器
# 会让用户选完才吃一个报错。
DOC_FILTER = "面经文件 (*.pdf *.docx *.txt *.md *.markdown);;所有文件 (*.*)"
IMG_FILTER = "截图 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)"


class InterviewPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None
        self._extract_worker = None
        #: 本页起过的全部后台 worker（销毁时统一断连，见 `_cleanup_workers`）。
        self._workers: list = []
        # 模拟面试状态
        self._session_id = None
        self._sim_resume_id = None
        self._sim_jd_id = None
        self._questions = []
        self._answers = []
        self._feedbacks = []
        self._current_question = ""
        # 「面试是否正在进行」——与「有没有选简历」分开的一个独立维度。
        # 旧版把两者混在 _sync_resume_state 里，导致面试进行中按钮被重新点亮（见下）。
        self._sim_running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "面试准备",
            "导入面经 → 生成面试题（带能脱稿讲的参考答案）→ 和 AI 模拟面试多轮演练。",
        ))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_article_tab(), "面经库")
        self.tabs.addTab(self._build_questions_tab(), "面试题生成")
        self.tabs.addTab(self._build_simulate_tab(), "模拟面试")
        layout.addWidget(self.tabs, 1)

        # 换简历时立刻刷新按钮可用性（不用等下一次 showEvent）
        self.q_resume_combo.currentIndexChanged.connect(self._sync_resume_state)
        self.sim_resume_combo.currentIndexChanged.connect(self._sync_resume_state)

        self.overlay = BusyOverlay(self)
        self._refresh_articles()

        # 页面销毁时断开仍在跑的后台任务回调，防止回调访问已释放的 Qt 对象。
        # 注：InterviewPage 是 QStackedWidget 的子页面，parent 关闭时不会触发它的
        # closeEvent，destroyed 才是子 widget 确定性析构钩子（与 `agent.py` 同款）。
        self.destroyed.connect(self._cleanup_workers)

    def _cleanup_workers(self) -> None:
        """页面销毁前断开所有后台任务回调。

        不 wait()/terminate()：worker 正在做网络请求，GUI 线程 wait 会卡住界面，
        terminate 不安全。断开信号后回调不再触发；已经排进事件队列的那一次
        由各回调入口的 `isValid(self)` 守卫兜住。
        """
        for w in self._workers:
            try:
                w.success.disconnect()
                w.failed.disconnect()
            except Exception:  # noqa: BLE001 —— 测试替身可能没有 disconnect
                pass
        self._workers = []

    # ================= 面经库 =================

    def _build_article_tab(self) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(12, 10, 12, 10)

        left = card()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(12, 10, 12, 10)
        ll.addWidget(section_label("导入面经"))
        self.art_company = QLineEdit(placeholderText="公司（如：字节跳动）")
        self.art_position = QLineEdit(placeholderText="岗位（可空）")
        self.art_content = QPlainTextEdit()
        self.art_content.setPlaceholderText("粘贴面经全文：面试流程、被问的问题、面试体验…\n（支持粘贴论坛帖子 / 牛客网面经）")
        self.art_content.setFixedHeight(220)
        import_btn = primary_button("导入面经（自动查重）")
        import_btn.clicked.connect(self._import_article)
        ll.addWidget(self.art_company)
        ll.addWidget(self.art_position)
        ll.addWidget(self.art_content)

        # 从文件 / 截图导入：本机解析成文字后**先填进上面的文本框**，
        # 由用户核对（OCR 会有错字）再点「导入面经」——而不是解析完直接入库，
        # 免得识别歪了的数据静默进库、还得去面经库里删。
        pick_row = QHBoxLayout()
        pick_row.setSpacing(8)
        self.art_file_btn = QPushButton("选文件导入")
        self.art_file_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.art_file_btn.setToolTip("支持 PDF / Word(docx) / TXT / Markdown，在本机解析")
        self.art_file_btn.clicked.connect(lambda: self._import_from_path(is_image=False))
        self.art_ocr_btn = QPushButton("选截图 OCR 导入")
        self.art_ocr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.art_ocr_btn.setToolTip("支持 PNG / JPG / BMP / WebP，走 Windows 系统 OCR")
        self.art_ocr_btn.clicked.connect(lambda: self._import_from_path(is_image=True))
        pick_row.addWidget(self.art_file_btn)
        pick_row.addWidget(self.art_ocr_btn)
        pick_row.addStretch(1)
        ll.addLayout(pick_row)
        self.art_extract_hint = hint_label(
            "文件与截图都在本机解析（截图走系统 OCR），只把识别出的文字入库，原文件不上传。")
        self.art_extract_hint.setWordWrap(True)
        ll.addWidget(self.art_extract_hint)

        ll.addWidget(import_btn, 0, Qt.AlignmentFlag.AlignLeft)
        # ⚠️ 必须留一个底部 stretch：本卡片被父 HBoxLayout(stretch=2) 撑到整行高，
        # 若末尾没有 stretch，多出的竖向空间会被平摊给「Preferred 且无高度约束的 QLabel」
        # （「导入面经」标题 + 底部提示），把它们各自拉成 ~140px、文字垂直居中 →
        # 标题上下出现大段空白。加 stretch 后多余空间沉到底部，标题回到自然高度。
        ll.addStretch(1)
        layout.addWidget(left, 2)

        right = card()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        head.addWidget(section_label("我的面经库"))
        del_btn = danger_button("删除选中")
        del_btn.clicked.connect(self._delete_article)
        head.addStretch(1)
        head.addWidget(del_btn)
        rl.addLayout(head)
        self.art_table = QTableWidget(0, 3)
        self.art_table.setHorizontalHeaderLabels(["公司", "岗位", "导入时间"])
        self.art_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.art_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.art_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.art_table.itemDoubleClicked.connect(self._view_article)
        rl.addWidget(self.art_table)
        self.art_view = QTextEdit()
        self.art_view.setReadOnly(True)
        self.art_view.setFixedHeight(180)
        rl.addWidget(hint_label("双击列表查看面经全文（在「面试题生成」中作为参考）"))
        rl.addWidget(self.art_view)
        layout.addWidget(right, 3)
        return host

    def _refresh_articles(self) -> None:
        articles = db.list_articles()
        self.art_table.setRowCount(len(articles))
        for i, a in enumerate(articles):
            self.art_table.setItem(i, 0, QTableWidgetItem(a.get("company") or ""))
            self.art_table.setItem(i, 1, QTableWidgetItem(a.get("position") or ""))
            self.art_table.setItem(i, 2, QTableWidgetItem((a.get("created_at") or "")[:10]))

    def _import_article(self) -> None:
        company = self.art_company.text().strip()
        content = self.art_content.toPlainText().strip()
        if not company or not content:
            QMessageBox.warning(self, "缺少信息", "请填写公司并粘贴面经内容")
            return
        result = db.save_article(company, content, self.art_position.text().strip())
        if result.get("duplicate"):
            QMessageBox.information(self, "重复导入", "检测到相似度 ≥ 90% 的面经已存在，已跳过导入。")
            return
        if result.get("queued"):
            # 离线导入：抽题这一步现在**本机也能做**，所以要说清到底抽到没抽到 ——
            # 只说「联网后抽取」会让用户以为现在看到的题目列表是空的（其实可能已经有）。
            n = len(result.get("questions") or [])
            tip = (f"已在本机抽出 {n} 道题；联网后会自动同步到服务器。"
                   if n else "题目与标签会在联网同步时补齐。")
            QMessageBox.information(
                self, "已保存（离线）", "面经原文已存在本地。" + tip)
        self.art_content.clear()
        self.art_extract_hint.setText(
            "文件与截图都在本机解析（截图走系统 OCR），只把识别出的文字入库，原文件不上传。")
        self._refresh_articles()
        self.mw.refresh_status()

    # ---- 从文件 / 截图导入（本机解析，原文件不出本机）----

    def _import_from_path(self, is_image: bool) -> None:
        """选文件/截图 → 本机解析 → 把识别出的文字**填进文本框**，不自动入库。

        为什么先填框再让用户点导入：OCR 一定会出错字，直接入库等于把
        识别噪声当成用户数据写进去；填框能让用户先扫一眼再决定。
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择截图（OCR 识别）" if is_image else "选择面经文件",
            "",
            IMG_FILTER if is_image else DOC_FILTER,
        )
        if not path:
            return
        self.art_file_btn.setEnabled(False)
        self.art_ocr_btn.setEnabled(False)
        self.overlay.show_over("正在本机识别截图（OCR）…" if is_image
                               else "正在本机解析文件…")

        def do_parse(p=path):
            # 懒导入：`local_ai` 会连带拉起 embedder/index 等重依赖，
            # 页面导入期不该付这个代价（`ui/import_resume.py` 同款处理）。
            from ...local_ai import parse_document
            return parse_document(p)

        self._extract_worker = LLMWorker(do_parse)
        self._workers.append(self._extract_worker)
        self._extract_worker.success.connect(
            lambda res, p=path: self._on_extracted(p, res))
        self._extract_worker.failed.connect(self._on_extract_failed)
        self._extract_worker.start()

    def _on_extracted(self, path: str, result) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self.art_file_btn.setEnabled(True)
        self.art_ocr_btn.setEnabled(True)
        text = str(getattr(result, "text", "") or "").strip()
        engine = str(getattr(result, "engine", "") or "")
        note = str(getattr(result, "note", "") or "")
        if not text:
            QMessageBox.warning(
                self, "没识别出文字",
                note or "这个文件里没有可用的文字（可能是扫描件且本机 OCR 不可用）。")
            return
        # **追加**而不是覆盖：用户可能已经粘了一段，别把他粘的冲掉
        old = self.art_content.toPlainText().strip()
        self.art_content.setPlainText(f"{old}\n{text}".strip() if old else text)
        bits = [f"已识别 {len(text)} 字（{engine or '未知引擎'}）"]
        if note:
            bits.append(note)
        if not self.art_company.text().strip():
            bits.append("请补填公司名，核对文字后点「导入面经」")
        else:
            bits.append("核对文字后点「导入面经」")
        self.art_extract_hint.setText("　".join(bits))

    def _on_extract_failed(self, msg: str) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self.art_file_btn.setEnabled(True)
        self.art_ocr_btn.setEnabled(True)
        QMessageBox.warning(self, "识别失败", msg)

    def _delete_article(self) -> None:
        row = self.art_table.currentRow()
        if row < 0:
            return
        articles = db.list_articles()
        if row >= len(articles):
            return
        a = articles[row]
        if QMessageBox.question(self, "删除确认", f"确定删除「{a['company']}」的面经？") == QMessageBox.StandardButton.Yes:
            # 不静默吞掉失败：P4 之后离线删除是「本地先删 + 排队上行」，
            # 所以这里不再拒绝用户，但必须说清「还没同步到服务器」。
            try:
                db.delete_article(a["id"])
            except db.ApiOfflineError:
                QMessageBox.information(
                    self, "已离线删除",
                    "网络不可用：这条面经已从本地移除，联网后会自动同步删除。")
            except db.ApiError as exc:
                QMessageBox.warning(self, "删除失败", str(exc))
                return
            self._refresh_articles()

    def _view_article(self, item) -> None:
        row = item.row()
        articles = db.list_articles()
        if row < len(articles):
            self.art_view.setPlainText(articles[row].get("raw_content") or "")

    # ================= 面试题生成 =================

    def _build_questions_tab(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        selector = card(highlight=True)
        sv = QVBoxLayout(selector)
        sv.setContentsMargins(16, 14, 16, 14)
        sv.setSpacing(8)
        sl = QHBoxLayout()
        sl.addWidget(QLabel("简历"))
        self.q_resume_combo = QComboBox()
        self.q_resume_combo.setMinimumWidth(150)
        sl.addWidget(self.q_resume_combo, 2)
        sl.addWidget(QLabel("目标岗位"))
        self.q_jd_combo = QComboBox()
        self.q_jd_combo.setMinimumWidth(150)
        sl.addWidget(self.q_jd_combo, 2)
        sl.addWidget(QLabel("面经参考"))
        self.q_article_combo = QComboBox()
        self.q_article_combo.setMinimumWidth(110)
        sl.addWidget(self.q_article_combo, 2)
        self.q_limit = QComboBox()
        for n in (5, 8, 10, 15):
            self.q_limit.addItem(f"{n} 题", n)
        sl.addWidget(self.q_limit)
        self.q_gen_btn = primary_button("生成面试题")
        self.q_gen_btn.clicked.connect(self._generate_questions)
        sl.addWidget(self.q_gen_btn)
        sv.addLayout(sl)

        # 没有简历时不要"点了才报错"：给一条引导 + 就地导入入口
        row2 = QHBoxLayout()
        self.q_resume_hint = resume_empty_hint(
            "还没有简历 —— 点右侧「导入简历」上传一份 Word / PDF（本机解析，不上传原文件），"
            "这里立刻就能选它，其他模块也会自动看到。")
        row2.addWidget(self.q_resume_hint, 1)
        row2.addWidget(make_import_button(self, self._on_resume_imported))
        sv.addLayout(row2)
        layout.addWidget(selector)
        layout.addWidget(hint_label("参考答案带「数字保真」标记：答案里的量化数字若不在你简历里，会被标出提醒你核实。"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.q_result_host = QWidget()
        self.q_result_layout = QVBoxLayout(self.q_result_host)
        self.q_result_layout.setContentsMargins(0, 0, 0, 0)
        self.q_result_layout.addWidget(empty_state(
            "还没有面试题",
            "选择简历/岗位（可选面经）后点「生成面试题」，题目与参考答案会出现在这里"))
        scroll.setWidget(self.q_result_host)
        layout.addWidget(scroll, 1)
        return host

    def _refresh_combos(self, select_resume: str | None = None) -> None:
        resumes = db.list_resumes()
        self._fill_resume_combo(self.q_resume_combo, resumes, keep=select_resume)
        self._fill_resume_combo(self.sim_resume_combo, resumes, keep=select_resume)
        jds = db.list_jds()
        self.q_jd_combo.clear()
        if not jds:
            self.q_jd_combo.addItem("（暂无岗位）", None)
            self.q_jd_combo.setToolTip("请先到「岗位分析 · 解析职位」标签解析岗位")
        for j in jds:
            company = ((j.get("company") or "?").replace("\n", " ").strip())[:12]
            position = ((j.get("position") or "?").replace("\n", " ").strip())[:18]
            self.q_jd_combo.addItem(f"{company}·{position}", j["id"])
        articles = db.list_articles()
        self.q_article_combo.clear()
        self.q_article_combo.addItem("（不使用面经）", "")
        for a in articles:
            self.q_article_combo.addItem((a.get("company") or "面经")[:10], a["id"])
        self._sync_resume_state()

    def _fill_resume_combo(self, combo: QComboBox, resumes: list,
                           keep: str | None = None) -> None:
        """填充简历下拉；`keep` 指定要恢复选中的 id（默认沿用当前选中）。

        `clear()` 会丢掉当前选中，所以重建后要按 id 找回来 —— 否则每次
        showEvent 都会把用户刚选的简历重置成第一份。
        """
        want = keep if keep is not None else combo.currentData()
        combo.clear()
        if not resumes:
            combo.addItem("（暂无简历）", None)
            combo.setToolTip("还没有简历：点右侧「导入简历」上传一份 Word / PDF")
        for r in resumes:
            pj = r.get("parsed_json") or {}
            is_opt = (r.get("source") == "optimized") or (pj.get("kind") == "optimized")
            if is_opt:
                tjd = pj.get("target_jd") or {}
                tpos = (tjd.get("position") or pj.get("position") or "").strip()
                tco = (tjd.get("company") or "").strip()
                target = "·".join(p for p in (tco, tpos) if p)
                if target:
                    label = f"优化稿→{target}"
                else:
                    label = "优化稿（岗位未知）"
            else:
                label = (pj.get("position") or r.get("raw_text") or "")[:20]
                label = (label or "").replace("\n", " ").replace("\r", " ").strip()
            combo.addItem(label[:18] if label else "（未命名简历）", r["id"])
        if want:
            idx = combo.findData(want)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _sync_resume_state(self) -> None:
        """同步引导条与主按钮：缺简历直接禁用；**且面试进行中不允许再开一场**。

        刻意把两个维度分开：`has_sim`（材料是否可用）与 `_sim_running`（会话是否进行中）。
        旧版只看简历就把按钮置为可用，于是 `_on_first_question` 一回调，正在进行的面试里
        按钮又被点亮 → 能再点一次「开始模拟面试」，把 `_questions/_answers` 就地清零、
        另起一个会话，前一场永远不会收口（点评与总结会串台）。
        """
        has_resume = bool(self.q_resume_combo.currentData())
        self.q_resume_hint.setVisible(not has_resume)
        self.q_gen_btn.setEnabled(has_resume)
        has_sim = bool(self.sim_resume_combo.currentData())
        self.sim_resume_hint.setVisible(not has_sim)
        self.sim_start_btn.setEnabled(has_sim and not self._sim_running)

    def _on_resume_imported(self, resume_id: str) -> None:
        """来自「导入简历」的回调：等入库成功后再刷新，并把新简历选中。"""
        self._refresh_combos(select_resume=resume_id)

    def _generate_questions(self) -> None:
        resume_id = self.q_resume_combo.currentData()
        jd_id = self.q_jd_combo.currentData()
        if not resume_id or not jd_id:
            QMessageBox.warning(self, "缺少选择", "请先选择简历和目标岗位")
            return
        resume = db.get_resume(resume_id)
        jd = db.get_jd(jd_id)
        article_id = self.q_article_combo.currentData()
        limit = self.q_limit.currentData()

        def do():
            return db.generate_interview_questions(resume_id, jd_id, article_id, limit)

        self.q_gen_btn.setEnabled(False)
        self.overlay.show_over("AI 正在生成面试题与参考答案…")
        self._worker = LLMWorker(do)
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_questions)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_questions(self, questions) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self._sync_resume_state()
        while self.q_result_layout.count():
            item = self.q_result_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not questions:
            self.q_result_layout.addWidget(hint_label("未生成题目，请重试"))
            return
        for i, q in enumerate(questions, 1):
            qcard = card()
            ql = QVBoxLayout(qcard)
            ql.setContentsMargins(16, 14, 16, 14)
            head = QHBoxLayout()
            title = QLabel(f"Q{i}. {q.get('question', '')}")
            title.setStyleSheet("font-size:14px; font-weight:600;")
            title.setWordWrap(True)
            head.addWidget(title, 1)
            cat_color = {"技术": "#1d4ed8", "项目": "#7c3aed", "行为": "#0891b2"}.get(q.get("category"), "#64748b")
            head.addWidget(tag_label(f"{q.get('category', '')} · {q.get('difficulty', '')}", cat_color))
            ql.addLayout(head)
            answer = QLabel(q.get("answer", ""))
            answer.setWordWrap(True)
            answer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ql.addWidget(answer)
            if q.get("suspicious_numbers"):
                warn = QLabel("⚠ 疑似简历中没有的数字（请核实）：" + "、".join(q["suspicious_numbers"]))
                warn.setProperty("class", "text-warn")
                warn.setWordWrap(True)
                ql.addWidget(warn)
            self.q_result_layout.addWidget(qcard)
        self.q_result_layout.addStretch(1)
        self.mw.refresh_status()

    def _on_failed(self, msg: str) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self._sync_resume_state()
        QMessageBox.warning(self, "操作失败", msg)

    # ================= 模拟面试 =================

    def _build_simulate_tab(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        selector = card(highlight=True)
        sv = QVBoxLayout(selector)
        sv.setContentsMargins(16, 14, 16, 14)
        sv.setSpacing(8)
        sl = QHBoxLayout()
        sl.addWidget(QLabel("简历"))
        self.sim_resume_combo = QComboBox()
        sl.addWidget(self.sim_resume_combo, 2)
        sl.addWidget(QLabel("岗位 JD"))
        self.sim_jd_combo = QComboBox()
        sl.addWidget(self.sim_jd_combo, 2)
        self.sim_start_btn = primary_button("开始模拟面试")
        self.sim_start_btn.clicked.connect(self._start_simulate)
        self.sim_finish_btn = danger_button("提前结束并总结")
        self.sim_finish_btn.clicked.connect(self._finish_simulate)
        self.sim_finish_btn.setEnabled(False)
        sl.addWidget(self.sim_start_btn)
        sl.addWidget(self.sim_finish_btn)
        sv.addLayout(sl)

        row2 = QHBoxLayout()
        self.sim_resume_hint = resume_empty_hint(
            "还没有简历 —— 点右侧「导入简历」上传一份，就能开始模拟面试。")
        row2.addWidget(self.sim_resume_hint, 1)
        row2.addWidget(make_import_button(self, self._on_resume_imported))
        sv.addLayout(row2)
        layout.addWidget(selector)

        chat_card = card()
        cl = QVBoxLayout(chat_card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.addWidget(section_label("面试对话"))
        self.sim_chat = QTextEdit()
        self.sim_chat.setReadOnly(True)
        self.sim_chat.setStyleSheet("font-size:13px;")
        self.sim_chat.setPlaceholderText(
            "点「开始模拟面试」后，面试官的提问会出现在这里，你在下面输入回答即可。")
        cl.addWidget(self.sim_chat, 1)
        input_row = QHBoxLayout()
        self.sim_answer = QPlainTextEdit()
        self.sim_answer.setPlaceholderText("输入你的回答（多行）…")
        self.sim_answer.setFixedHeight(72)
        input_row.addWidget(self.sim_answer, 1)
        self.sim_send_btn = primary_button("提交回答")
        self.sim_send_btn.clicked.connect(self._submit_answer)
        self.sim_send_btn.setEnabled(False)
        input_row.addWidget(self.sim_send_btn, 0, Qt.AlignmentFlag.AlignTop)
        cl.addLayout(input_row)
        layout.addWidget(chat_card, 1)
        return host

    def _start_simulate(self) -> None:
        resume_id = self.sim_resume_combo.currentData()
        jd_id = self.sim_jd_combo.currentData()
        if not resume_id or not jd_id:
            QMessageBox.warning(self, "缺少选择", "请选择简历和岗位 JD")
            return
        self._sim_resume_id = resume_id
        self._sim_jd_id = jd_id
        self._session_id = None
        self._questions, self._answers, self._feedbacks = [], [], []
        self.sim_chat.clear()
        self.sim_answer.clear()
        self.sim_send_btn.setEnabled(False)
        self.sim_finish_btn.setEnabled(False)
        self.sim_start_btn.setEnabled(False)
        self._sim_running = True
        self.overlay.show_over("AI 面试官正在出第一题…")

        def do():
            return db.start_interview_simulate(resume_id, jd_id)

        self._worker = LLMWorker(do)
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_first_question)
        # 起会话失败要解除「进行中」，否则按钮被永久卡死；与其它操作的失败分开接，
        # 避免「提交回答失败」也被误当成整场结束而放开重开。
        self._worker.failed.connect(self._on_simulate_start_failed)
        self._worker.start()

    def _on_simulate_start_failed(self, msg: str) -> None:
        if not isValid(self):
            return
        self._sim_running = False
        self._on_failed(msg)

    def _on_first_question(self, result: dict) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self._sync_resume_state()
        self._session_id = result.get("session_id")
        question = result.get("question", "")
        self._current_question = question
        self._questions.append(question)
        self.sim_chat.append(f"【面试官】{question}")
        self.sim_send_btn.setEnabled(True)
        self.sim_finish_btn.setEnabled(True)

    def _submit_answer(self) -> None:
        answer = self.sim_answer.toPlainText().strip()
        if not answer or not self._current_question:
            return
        self.sim_chat.append(f"\n【我】{answer}")
        self.sim_answer.clear()
        self._answers.append(answer)
        self.sim_send_btn.setEnabled(False)
        self.overlay.show_over("AI 正在点评并准备下一问…")

        def do():
            return db.submit_interview_answer(self._session_id, answer)

        self._worker = LLMWorker(do)
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_feedback)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_feedback(self, result: dict) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        feedback = result.get("feedback", "")
        next_q = result.get("next_question")
        is_finished = result.get("is_finished", False)
        self._feedbacks.append(feedback)
        self.sim_chat.append(f"【点评】{feedback}")
        if next_q:
            self._current_question = next_q
            self._questions.append(next_q)
            self.sim_chat.append(f"\n【面试官】{next_q}")
            self.sim_send_btn.setEnabled(True)
        if is_finished:
            self.sim_send_btn.setEnabled(False)
            self._finish_simulate()

    def _finish_simulate(self) -> None:
        if not self._questions or not self._answers:
            QMessageBox.information(self, "提示", "还没有回答任何问题")
            return
        self.sim_send_btn.setEnabled(False)
        self.sim_finish_btn.setEnabled(False)
        self.overlay.show_over("AI 正在生成整场面试总结…")

        def do():
            db.finish_interview_simulate(self._session_id)
            return db.get_interview_summary(self._session_id)

        self._worker = LLMWorker(api_guard(do))
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_summary)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_summary(self, result) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        summary = result.get("summary") if isinstance(result, dict) else str(result)
        self.sim_chat.append(f"\n{'=' * 40}\n【整场总结】\n{summary}")
        # 一场结束：解除「进行中」，让「开始模拟面试」重新可用 —— 不用先切页再回来。
        self._sim_running = False
        self._sync_resume_state()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_combos()