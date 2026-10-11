"""岗位匹配页：选简历 + 选岗位 → 匹配度评分与差距明细 → 匹配排行。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QAbstractItemView,
)

from ... import api_client as db
from ...llm import LLMWorker
from ..components import (
    BusyOverlay, card, empty_state, flow_tags, hint_label, make_title,
    primary_button,
    score_badge, section_label,
)
from ..import_resume import make_import_button, resume_empty_hint


class MatchPage(QWidget):
    def __init__(self, main_window, with_title: bool = True):
        super().__init__()
        self.mw = main_window
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        if with_title:
            layout.addWidget(make_title(
                "岗位匹配",
                "技能词典 × 语义覆盖度混合评分：逐条判定 JD 需求的命中 / 近似 / 缺失，"
                "并给出简历里的证据句。匹配度 ≥ 80 建议直接投递。",
            ))

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ===== 上：匹配操作与结果 =====
        top = QWidget()
        top_layout = QVBoxLayout(top)
        selector_card = card(highlight=True)
        sv = QVBoxLayout(selector_card)
        sv.setContentsMargins(16, 14, 16, 14)
        sv.setSpacing(8)
        sl = QHBoxLayout()
        sl.addWidget(QLabel("选择简历"))
        self.resume_combo = QComboBox()
        self.resume_combo.setMinimumWidth(180)
        sl.addWidget(self.resume_combo, 2)
        sl.addWidget(QLabel("选择岗位"))
        self.jd_combo = QComboBox()
        self.jd_combo.setMinimumWidth(180)
        sl.addWidget(self.jd_combo, 2)
        self.match_btn = primary_button("计算匹配度")
        self.match_btn.clicked.connect(self._match)
        sl.addWidget(self.match_btn)
        self.batch_btn = QPushButton("批量匹配全部岗位")
        self.batch_btn.setToolTip("用当前选中的简历，一次性匹配「岗位分析 · 解析职位」标签里的全部岗位，按匹配度降序")
        self.batch_btn.clicked.connect(self._batch_match)
        sl.addWidget(self.batch_btn)
        sv.addLayout(sl)

        # 没有简历时不要"点了才报错"：给一条引导 + 就地导入入口
        row2 = QHBoxLayout()
        self.resume_hint = resume_empty_hint(
            "还没有简历 —— 点右侧「导入简历」上传一份 Word / PDF（本机解析，不上传原文件）。")
        row2.addWidget(self.resume_hint, 1)
        row2.addWidget(make_import_button(self, self._on_resume_imported))
        sv.addLayout(row2)
        top_layout.addWidget(selector_card)

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
        top_layout.addWidget(self.result_card, 1)
        self.result_inner.addWidget(empty_state(
            "还没有匹配结果",
            "选择简历与岗位后点「计算匹配度」，这里会逐条列出命中 / 近似 / 缺失及证据句"))
        splitter.addWidget(top)

        # ===== 下：匹配排行 =====
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        head = QHBoxLayout()
        head.addWidget(section_label("匹配排行（按匹配度降序）"))
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        head.addStretch(1)
        head.addWidget(refresh_btn)
        bottom_layout.addLayout(head)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["匹配度", "公司", "职位", "简历", "建议"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        bottom_layout.addWidget(self.table)
        splitter.addWidget(bottom)
        splitter.setSizes([420, 260])

        layout.addWidget(splitter, 1)
        self.overlay = BusyOverlay(self)
        # 换简历时立刻刷新按钮可用性（不用等下一次刷新）
        self.resume_combo.currentIndexChanged.connect(self._sync_action_state)
        self._refresh()

    def _refresh(self, select_resume: str | None = None) -> None:
        resumes = db.list_resumes()
        want = select_resume if select_resume is not None else self.resume_combo.currentData()
        self.resume_combo.clear()
        if not resumes:
            self.resume_combo.addItem("（暂无简历）", None)
            self.resume_combo.setToolTip("还没有简历：点右侧「导入简历」上传一份 Word / PDF")
        for r in resumes:
            label = (r.get("parsed_json") or {}).get("position") or r["raw_text"][:24]
            label = (label or "").replace("\n", " ").replace("\r", " ").strip()
            self.resume_combo.addItem(label[:16] if label else "（未命名简历）", r["id"])
        if want:
            idx = self.resume_combo.findData(want)
            if idx >= 0:
                self.resume_combo.setCurrentIndex(idx)
        jds = db.list_jds()
        self.jd_combo.clear()
        if not jds:
            self.jd_combo.addItem("（暂无岗位）", None)
            self.jd_combo.setToolTip("请先到「岗位分析 · 解析职位」标签解析岗位")
        for j in jds:
            company = ((j.get("company") or "?").replace("\n", " ").strip())[:12]
            position = ((j.get("position") or "?").replace("\n", " ").strip())[:18]
            self.jd_combo.addItem(f"{company} · {position}", j["id"])
        self._sync_action_state()
        self._refresh_table()

    def _sync_action_state(self) -> None:
        """按「有没有简历」同步引导条与主按钮：缺简历直接禁用，而不是点了才弹错。"""
        has_resume = bool(self.resume_combo.currentData())
        self.resume_hint.setVisible(not has_resume)
        self.match_btn.setEnabled(has_resume)
        self.batch_btn.setEnabled(has_resume)

    def _on_resume_imported(self, resume_id: str) -> None:
        """来自「导入简历」的回调：等入库成功后再刷新，并把新简历选中。"""
        self._refresh(select_resume=resume_id)

    def _refresh_table(self) -> None:
        matches = sorted(db.list_matches(), key=lambda m: -(m.get("score") or 0))
        resumes = {r["id"]: r for r in db.list_resumes()}
        self.table.setRowCount(len(matches))
        for i, m in enumerate(matches):
            score_item = QTableWidgetItem(str(m.get("score", 0)))
            self.table.setItem(i, 0, score_item)
            self.table.setItem(i, 1, QTableWidgetItem(m.get("company") or "未知"))
            self.table.setItem(i, 2, QTableWidgetItem(m.get("position") or "未知"))
            resume = resumes.get(m.get("resume_id"))
            rlabel = ((resume or {}).get("parsed_json") or {}).get("position") or (resume or {}).get("raw_text", "")[:16]
            rlabel = (rlabel or "").replace("\n", " ").replace("\r", " ").strip()
            self.table.setItem(i, 3, QTableWidgetItem(rlabel[:16]))
            self.table.setItem(i, 4, QTableWidgetItem(m.get("suggestion") or ""))
        self.mw.refresh_status()

    def _match(self) -> None:
        resume_id = self.resume_combo.currentData()
        jd_id = self.jd_combo.currentData()
        if not resume_id or not jd_id:
            QMessageBox.warning(self, "缺少选择", "还没有简历的话，点右侧「导入简历」上传一份；岗位可在「岗位分析 · 解析职位」标签解析")
            return
        resume = db.get_resume(resume_id)
        jd = db.get_jd(jd_id)
        if not resume or not jd:
            return
        self.match_btn.setEnabled(False)
        self.overlay.show_over("正在计算匹配度…")
        self._worker = LLMWorker(lambda: db.calculate_match(jd_id, resume_id))
        self._worker.success.connect(lambda result: self._on_matched(resume, jd, result))
        self._worker.failed.connect(self._on_match_failed)
        self._worker.start()

    def _batch_match(self) -> None:
        """批量匹配：当前简历 × 全部岗位，按匹配度降序。"""
        resume_id = self.resume_combo.currentData()
        if not resume_id:
            QMessageBox.warning(self, "缺少选择", "还没有简历的话，点右侧「导入简历」上传一份")
            return
        resume = db.get_resume(resume_id)
        jds = db.list_jds()
        if not resume:
            QMessageBox.warning(self, "缺少选择", "还没有简历的话，点右侧「导入简历」上传一份")
            return
        if not jds:
            QMessageBox.warning(self, "缺少数据", "请先到「岗位分析 · 解析职位」标签解析至少一个岗位")
            return
        self.batch_btn.setEnabled(False)
        self.overlay.show_over(f"正在批量匹配 {len(jds)} 个岗位…")

        def run():
            results = []
            for jd in jds:
                try:
                    r = db.calculate_match(jd["id"], resume_id)
                    results.append((jd, r))
                except Exception as e:  # noqa: BLE001
                    results.append((jd, {"score": 0, "suggestion": f"匹配失败：{e}"}))
            results.sort(key=lambda x: -(x[1].get("score") or 0))
            return results

        self._worker = LLMWorker(run)
        self._worker.success.connect(self._on_batch_matched)
        self._worker.failed.connect(self._on_match_failed)
        self._worker.start()

    def _on_batch_matched(self, results) -> None:
        self.overlay.hide()
        self._sync_action_state()
        self._refresh_table()
        top = results[0] if results else None
        top_score = (top[1].get("score") or 0) if top else 0
        QMessageBox.information(
            self, "批量匹配完成",
            f"已匹配 {len(results)} 个岗位，按匹配度降序排列。\n"
            f"最高匹配度：{top_score} 分",
        )

    def _on_match_failed(self, msg: str) -> None:
        self.overlay.hide()
        self._sync_action_state()
        QMessageBox.warning(self, "匹配失败", msg)

    def _on_matched(self, resume, jd, result: dict) -> None:
        self.overlay.hide()
        self._sync_action_state()
        self._show_result(jd, resume, result)
        self._refresh_table()

    def _show_result(self, jd, resume, result: dict) -> None:
        while self.result_inner.count():
            item = self.result_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        head_card = card(success=result.get("score", 0) >= 80,
                          warn=60 <= result.get("score", 0) < 80,
                          danger=result.get("score", 0) < 60)
        hl = QHBoxLayout(head_card)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.addWidget(score_badge(result.get("score", 0)))
        info = QVBoxLayout()
        title = QLabel(f"{jd.get('company')} · {jd.get('position')}")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        info.addWidget(title)
        info.addWidget(hint_label(result.get("suggestion", "")))
        hl.addLayout(info, 1)
        self.result_inner.addWidget(head_card)

        detail = result.get("skill_match_detail") or {}
        cols = QHBoxLayout()
        if detail.get("matched"):
            c1 = card(success=True)
            c1l = QVBoxLayout(c1)
            c1l.setContentsMargins(12, 10, 12, 10)
            c1l.addWidget(hint_label(f"已命中技能（{len(detail['matched'])}）"))
            c1l.addWidget(flow_tags(detail["matched"], "#059669"))
            cols.addWidget(c1, 1)
        if detail.get("missing"):
            c2 = card(danger=True)
            c2l = QVBoxLayout(c2)
            c2l.setContentsMargins(12, 10, 12, 10)
            c2l.addWidget(hint_label(f"缺失技能（{len(detail['missing'])}）"))
            c2l.addWidget(flow_tags(detail["missing"], "#dc2626"))
            cols.addWidget(c2, 1)
        if detail.get("partial"):
            c3 = card(warn=True)
            c3l = QVBoxLayout(c3)
            c3l.setContentsMargins(12, 10, 12, 10)
            c3l.addWidget(hint_label(f"相关但需强化（{len(detail['partial'])}）"))
            c3l.addWidget(flow_tags(detail["partial"], "#d97706"))
            cols.addWidget(c3, 1)
        if detail.get("matched") or detail.get("missing") or detail.get("partial"):
            self.result_inner.addLayout(cols)

        # 逐条判定 + 证据：让分数「可解释」
        reqs = result.get("requirement_detail") or []
        if reqs:
            stats = result.get("stats") or {}
            explain = card()
            el = QVBoxLayout(explain)
            el.setContentsMargins(16, 14, 16, 14)
            head = QLabel(
                f"需求逐条判定（共 {stats.get('requirement_total', len(reqs))} 条："
                f"命中 {stats.get('matched', 0)}、近似 {stats.get('partial', 0)}、"
                f"缺失 {stats.get('missing', 0)}；需求覆盖度 "
                f"{int(round(stats.get('coverage', 0) * 100))}%）")
            head.setStyleSheet("font-weight:700;")
            head.setWordWrap(True)
            el.addWidget(head)

            evidence = result.get("evidence") or {}
            tones = {"matched": ("#059669", "命中"), "partial": ("#d97706", "近似"),
                     "missing": ("#dc2626", "缺失")}

            for status in ("partial", "missing", "matched"):
                group = [r for r in reqs if r.get("status") == status]
                if not group:
                    continue
                tone, tag = tones[status]
                el.addWidget(hint_label(f"—— {tag}（{len(group)}）——"))
                for req in group[:8]:
                    kind = {"must": "必备", "nice": "加分", "tech": "技术栈",
                            "raw": "原文"}.get(req.get("kind"), "")
                    row = QLabel(f"· [{kind}] {req.get('text', '')}")
                    row.setStyleSheet(f"color:{tone};")
                    row.setWordWrap(True)
                    el.addWidget(row)

                    if status == "partial":
                        terms = req.get("terms") or []
                        if terms:
                            el.addWidget(hint_label("　依据：" + "、".join(terms)))
                        ev = evidence.get(req.get("text"))
                        if ev:
                            ev_label = QLabel("　证据：" + ev)
                            ev_label.setProperty("class", "muted")
                            ev_label.setWordWrap(True)
                            el.addWidget(ev_label)
                        miss = req.get("missing_skills") or []
                        if miss:
                            el.addWidget(hint_label("　这条要求里还缺：" + "、".join(miss)))
                    elif status == "missing":
                        miss = req.get("missing_skills") or []
                        if miss:
                            el.addWidget(hint_label("　涉及：" + "、".join(miss)))

                if len(group) > 8:
                    el.addWidget(hint_label(f"　（另有 {len(group) - 8} 条）"))
            self.result_inner.addWidget(explain)

        gap = result.get("gap_analysis") or {}
        if gap.get("suggestion"):
            self.result_inner.addWidget(hint_label("投递建议：" + gap["suggestion"]))
        self.result_inner.addStretch(1)