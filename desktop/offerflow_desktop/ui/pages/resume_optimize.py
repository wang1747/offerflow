"""简历优化页：选简历 + 选岗位 → 差距驱动整份重写 → diff 对比 → 保存新版本 → 导出。"""

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from ... import api_client as db
from ... import database as local_db      # 导出目录等本机配置仍存本地 SQLite
from ...core import pdf_verify
from ...core.resume_review import parse_resume_text
from ...core.sections import BODY_SECTIONS, section_labels_in_order
from ...llm import LLMWorker
from ...services import resume_generation as rg
from ...services import resume_optimizer as ro
from ...services import templates
from ...services.exporter import export_docx_from_blocks, export_txt
from ...services.pdf_exporter import export_pdf_from_blocks
from .. import charts
from .. import tokens
from ..components import (
    BusyOverlay, FlowWidget, api_guard, card, confirm_placeholders, hint_label,
    make_title, primary_button, retrieval_summary, section_label, set_prop,
    soft_chip, success_button,
)
from ..export_result import notify_export_result
# 「没有简历就地补一份」的共享入口（面试准备 / 岗位匹配 / 薪资谈判都接了，
# 唯独优化页此前只在下拉里写了句 tooltip，用户没有可点的路）
from ..import_resume import make_import_button, resume_empty_hint
from ..template_picker import (
    TemplatePickerDialog, load_export_options, save_export_options,
)

# JD 关键词命中三态色：绿=已命中 / 黄=措辞不同已对齐 / 红=原文缺失。
# 这是**数据语义色**（三态必须互相可辨），不参与 tokens 的视觉规范收敛；
# 与 components.status_chip 同样的做法：按主题在浅/深两套里取一套，
# 避免深色底上 #16a34a 这类浅色值对比度不足。
_HIT_TONES = {
    "matched": ("#16a34a", "#4ade80"),
    "partial": ("#d97706", "#fbbf24"),
    "missing": ("#dc2626", "#f87171"),
}


def _hit_tone(kind: str) -> str:
    light, dark = _HIT_TONES[kind]
    return dark if tokens.is_dark() else light


def _pct(value) -> str:
    """把服务端的 0~1 比例安全格式化成百分比。

    ⚠️ ``pres.get("score", 0)`` 在**键存在但值为 None** 时不会用默认值 ——
    直接 ``:.0%`` 会抛 ``TypeError``，把整个 ``_on_optimized`` 打断
    （遮罩已隐藏 → 结果区空白、按钮状态不更新）。
    """
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "—"


class ResumeOptimizePage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None
        self._result = None
        self._template_key = None          # 上次选过的版式，导出时记住
        # 外观与分区顺序独立于版式，从本机配置读回来（换版式不会丢）。
        self._appearance, self._order = load_export_options()
        self._resume = None
        self._jd = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "简历优化",
            "针对目标岗位做差距分析 + 整份重写。事实保真硬校验：关键信息绝不丢失，一页约束用「三刀裁剪」。",
        ))

        selector_card = card(highlight=True)
        sv = QVBoxLayout(selector_card)
        sv.setContentsMargins(16, 14, 16, 14)
        sv.setSpacing(10)
        sl = QHBoxLayout()
        sl.setSpacing(12)
        sl.addWidget(QLabel("简历"))
        self.resume_combo = QComboBox()
        self.resume_combo.setMinimumWidth(180)
        sl.addWidget(self.resume_combo, 2)
        sl.addWidget(QLabel("目标岗位"))
        self.jd_combo = QComboBox()
        self.jd_combo.setMinimumWidth(180)
        sl.addWidget(self.jd_combo, 2)
        self.optimize_btn = primary_button("开始优化")
        self.optimize_btn.clicked.connect(self._optimize)
        sl.addWidget(self.optimize_btn)
        self.agent_btn = QPushButton("用 Agent 制定行动")
        self.agent_btn.clicked.connect(self._open_agent)
        sl.addWidget(self.agent_btn)
        sv.addLayout(sl)

        # 第二行：缺简历时的引导 + 两条「建档」入口。
        #   * 「导入简历」——复用 ui/import_resume 的共享流程（本机解析 Word/PDF、
        #     只上行抽出来的文字），与面试准备/岗位匹配/薪资谈判是同一套实现；
        #   * 「去生成简历」——手上没有现成简历时的另一条路（从零做一份）。
        # 两条都给，是因为「手上有简历想改」和「压根没简历」是两类用户，
        # 只留一条会把人堵在死路上（网页版这里就是一个跳去生成页的链接）。
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.resume_hint = resume_empty_hint(
            "还没有简历 —— 点右侧「导入简历」上传一份 Word / PDF，"
            "或到「简历生成」从零做一份。")
        row2.addWidget(self.resume_hint, 1)
        self.import_btn = make_import_button(self, self._on_resume_imported)
        row2.addWidget(self.import_btn)
        self.gen_resume_btn = QPushButton("去生成简历")
        set_prop(self.gen_resume_btn, "class", "ghost")
        self.gen_resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gen_resume_btn.setToolTip("还没有简历？到「简历生成」从零做一份")
        self.gen_resume_btn.clicked.connect(lambda: self.mw.navigate("简历生成"))
        row2.addWidget(self.gen_resume_btn)
        sv.addLayout(row2)
        layout.addWidget(selector_card)

        self.tabs = QTabWidget()
        # 对比视图
        compare_host = QWidget()
        cl = QVBoxLayout(compare_host)
        cl.setContentsMargins(12, 10, 12, 10)
        split = QSplitter(Qt.Orientation.Horizontal)
        left_card = card()
        ll = QVBoxLayout(left_card)
        ll.setContentsMargins(12, 10, 12, 10)
        ll.addWidget(section_label("优化前"))
        self.before_view = QTextEdit()
        self.before_view.setReadOnly(True)
        self.before_view.setPlaceholderText("优化前的简历原文会显示在这里")
        ll.addWidget(self.before_view)
        split.addWidget(left_card)
        right_card = card()
        rl = QVBoxLayout(right_card)
        rl.setContentsMargins(12, 10, 12, 10)
        rl.addWidget(section_label("优化后"))
        self.after_view = QTextEdit()
        self.after_view.setReadOnly(True)
        self.after_view.setPlaceholderText("优化后的简历会显示在这里（点上方「开始优化」）")
        rl.addWidget(self.after_view)
        split.addWidget(right_card)
        split.setSizes([480, 480])
        # ATS 对比卡在最上面（对齐网页版把「优化前/优化后评分」放在结果区顶部），
        # 默认隐藏，拿到结果才显形 —— 空卡片摆在最显眼处会让人以为「本来就该有」。
        cl.addWidget(self._build_ats_card())
        cl.addWidget(split)
        self.tabs.addTab(compare_host, "前后对比")

        # 变更明细
        self.changes_view = QTextEdit()
        self.changes_view.setReadOnly(True)
        self.changes_view.setPlaceholderText("逐条变更明细（diff）会在优化后显示在这里")
        self.tabs.addTab(self.changes_view, "变更明细（diff）")

        # 质检（每条问题钉到「优化后」正文的那一行）
        review_host = QWidget()
        rvl = QVBoxLayout(review_host)
        rvl.setContentsMargins(12, 10, 12, 10)
        rvl.addWidget(hint_label(
            "每条问题都指向「优化后」正文的具体行号；点右侧按钮会切到"
            "「前后对比」并选中那一行。没有行号的是整篇级结论"
            "（缺邮箱、时间线倒挂这类），不对应某一行。"
        ))
        self.review_scope_label = hint_label("")
        rvl.addWidget(self.review_scope_label)
        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.review_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.review_inner = QWidget()
        self.review_layout = QVBoxLayout(self.review_inner)
        self.review_layout.setContentsMargins(0, 0, 6, 0)
        self.review_layout.setSpacing(8)
        self.review_scroll.setWidget(self.review_inner)
        rvl.addWidget(self.review_scroll, 1)
        self.review_tab = review_host
        self.tabs.addTab(review_host, "质检")
        self._render_review({})

        # 历史版本（可回滚到任意旧版，对齐网页版「历史版本」）
        versions_host = QWidget()
        vl = QVBoxLayout(versions_host)
        vl.setContentsMargins(12, 10, 12, 10)
        vhead = QHBoxLayout()
        vhead.addWidget(hint_label(
            "每保存或优化一次都会产生一个版本，可随时恢复到任意旧版；"
            "恢复前的当前内容也会留在历史里，可以再恢复回来。"
        ), 1)
        self.versions_refresh_btn = QPushButton("刷新")
        self.versions_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.versions_refresh_btn.clicked.connect(self._load_versions)
        vhead.addWidget(self.versions_refresh_btn)
        vl.addLayout(vhead)
        self.versions_scroll = QScrollArea()
        self.versions_scroll.setWidgetResizable(True)
        self.versions_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.versions_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.versions_inner = QWidget()
        self.versions_layout = QVBoxLayout(self.versions_inner)
        self.versions_layout.setContentsMargins(0, 0, 6, 0)
        self.versions_layout.setSpacing(8)
        self.versions_scroll.setWidget(self.versions_inner)
        vl.addWidget(self.versions_scroll, 1)
        self.versions_tab = versions_host
        self.tabs.addTab(versions_host, "历史版本")
        # 切到「历史版本」Tab 时才拉版本列表（构造期不发请求，见 _refresh 注释）
        self.tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self.tabs, 1)

        # 底部操作
        bottom = QHBoxLayout()
        self.save_btn = success_button("保存为新版本")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        self.export_btn = primary_button("导出 Word / PDF（可选版式）")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_with_picker)
        self.export_txt_btn = primary_button("导出纯文本")
        self.export_txt_btn.setEnabled(False)
        self.export_txt_btn.clicked.connect(lambda: self._export("txt"))
        self.status_label = hint_label("")
        bottom.addWidget(self.save_btn)
        bottom.addWidget(self.export_btn)
        bottom.addWidget(self.export_txt_btn)
        bottom.addWidget(self.status_label, 1)
        layout.addLayout(bottom)

        self.overlay = BusyOverlay(self)
        # **构造期不发网络请求**：两个下拉的数据（简历 / 岗位）走 api_client，
        # 构造期发请求会在页面提前销毁时让后台线程摸到已删除的 C++ 对象而崩进程
        # （同文件「版本列表」那段注释讲的是同一个危害）。这里只把初始状态摆好
        # ——"还没有简历"→ 引导条可见、主按钮置灰；真正的数据加载交给 showEvent。
        # 刻意**不调 `_sync_action_state()`**：构造期直接摆控件状态即可，
        # 那里读的 currentData() 此时必然为空，结果与这里完全一致。
        self.resume_hint.setVisible(True)
        self.optimize_btn.setEnabled(False)

    def showEvent(self, event):
        """回到本页时刷新下拉；若正停在「历史版本」Tab 就顺带刷新版本列表。

        数据加载放这里而不是构造期，见 ``__init__`` 末尾的说明。
        ``singleShot(0, ...)`` 让本页先画出来再发请求，避免首帧卡在白屏。
        """
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh)
        if self.tabs.currentWidget() is self.versions_tab:
            self._load_versions()

    def _refresh(self, select_resume: str | None = None) -> None:
        """重填两个下拉。

        ``select_resume`` 用「导入简历」成功后把新简历选中（对齐 match/salary）；
        不传时**保留当前选中的那份** —— 重填下拉会把 index 打回 0，回滚版本后
        刷新就悄悄换成了另一份简历（G7 顺带修掉的老毛病）。
        """
        want = select_resume or self.resume_combo.currentData()
        # 重填下拉会触发 currentIndexChanged，先屏蔽，末尾统一加载一次版本列表
        self.resume_combo.blockSignals(True)
        resumes = db.list_resumes()
        self.resume_combo.clear()
        if not resumes:
            self.resume_combo.addItem("（暂无简历）", None)
            self.resume_combo.setToolTip("还没有简历：点右侧「导入简历」上传一份，"
                                        "或到「简历生成」从零做一份")
        # id→resume 映射，供优化稿回溯「源自哪份原稿」用。
        resume_by_id = {r["id"]: r for r in resumes}
        for r in resumes:
            pj = r.get("parsed_json") or {}
            is_opt = (r.get("source") == "optimized") or (pj.get("kind") == "optimized")
            if is_opt:
                # 优化稿：明确标出「针对哪个岗位优化」，否则列表里和原稿混在一起
                # 让人分不清这份是为谁做的（用户反馈痛点）。
                tjd = pj.get("target_jd") or {}
                tpos = (tjd.get("position") or pj.get("position") or "").strip()
                tco = (tjd.get("company") or "").strip()
                target = "·".join(p for p in (tco, tpos) if p)
                parent = resume_by_id.get(r.get("parent_id") or "")
                parent_name = ""
                if parent:
                    ppj = parent.get("parsed_json") or {}
                    parent_name = (ppj.get("position")
                                   or parent.get("raw_text") or "")[:12]
                    parent_name = parent_name.replace("\n", " ").replace("\r", " ").strip()
                if target:
                    text = f"优化稿→{target}"
                    if parent_name:
                        text += f"（源自{parent_name}）"
                else:
                    text = "优化稿（岗位未知）"
            else:
                label = (pj.get("position") or r.get("raw_text") or "")[:24]
                label = (label or "").replace("\n", " ").replace("\r", " ").strip()
                ver = int(r.get("version") or 1)
                text = label[:16] or "（未命名简历）"
                if ver > 1:
                    text = f"{text} v{ver}"
            self.resume_combo.addItem(text, r["id"])
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
        self.resume_combo.blockSignals(False)
        if not getattr(self, "_version_signal_ready", False):
            self.resume_combo.currentIndexChanged.connect(self._load_versions)
            self._version_signal_ready = True
        self._sync_action_state()
        # 版本列表走网络请求，**只在「历史版本」Tab 可见时**才拉：
        # 构造期发请求会在页面提前销毁时崩进程（后台线程回调碰到已删除对象）。
        if self.tabs.currentWidget() is self.versions_tab:
            self._load_versions()
        else:
            self._clear_versions()
            self.versions_layout.addWidget(
                hint_label("切到本页会自动列出这份简历的历史版本。"))
            self.versions_layout.addStretch(1)

    def _sync_action_state(self) -> None:
        """按「有没有简历」同步引导条与主按钮：缺简历直接禁用，而不是点了才弹错。"""
        has_resume = bool(self.resume_combo.currentData())
        self.resume_hint.setVisible(not has_resume)
        self.optimize_btn.setEnabled(has_resume)

    def _on_resume_imported(self, resume_id: str) -> None:
        """「导入简历」成功后的回调：等入库成功再刷新，并把新简历选中。

        时序要点见 `ui/import_resume.import_resume_and_select`：必须等入库
        成功才刷新，否则下拉里还没有这一条，会出现「值是新的、选项里没有」。
        """
        self._refresh(select_resume=resume_id)

    def _on_tab_changed(self, _index: int) -> None:
        """切到「历史版本」Tab 时加载（首次进入 + 每次回来都刷新）。"""
        if self.tabs.currentWidget() is self.versions_tab:
            self._load_versions()

    # ===== ATS 兼容性对比（优化前 → 优化后）=====

    def _build_ats_card(self) -> QFrame:
        """「ATS 兼容性」前后对比卡。

        对齐网页版优化结果区顶部那三栏（优化前评分 / 优化后评分 / 关键词
        命中），桌面版合成一张卡：左边**同尺**对比条，右边两份结论列表，
        底部关键词命中三态标签。

        分数取自结果里的 ``ats`` / ``ats_after``，**页面不自己重算** ——
        两次分数必须出自**同一条规则**才可比：本地优化链路用
        ``core/ats.check_ats_compatibility`` 对「原文」与「优化稿」各跑一次，
        回落云端时由服务端跑同一套规则。两条来源都只产出这一对数值，
        页面照渲染即可；`None` 表示**没有这份数据**（不是 0 分），整块收起。
        """
        self.ats_card = card()
        root = QVBoxLayout(self.ats_card)
        root.setContentsMargins(*tokens.CARD_MARGINS)
        root.setSpacing(tokens.SP_SM)

        head = QHBoxLayout()
        head.setSpacing(tokens.SP_SM)
        head.addWidget(section_label("ATS 兼容性"))
        self.ats_delta_label = QLabel("")
        self.ats_delta_label.setToolTip(
            "同一条 ATS 规则分别跑「原文」与「优化稿」，两次分数因此可比。")
        head.addWidget(self.ats_delta_label)
        head.addStretch(1)
        self.ats_hit_label = hint_label("")
        head.addWidget(self.ats_hit_label)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(tokens.SP_LG)
        self.ats_chart = charts.ScoreCompare()
        body.addWidget(self.ats_chart, 3)

        cols = QHBoxLayout()
        cols.setSpacing(tokens.SP_MD)
        self.ats_issues_col, self.ats_issues_layout = self._ats_list_col("优化前的问题")
        self.ats_sugg_col, self.ats_suggestions_layout = self._ats_list_col("优化后的改进项")
        cols.addWidget(self.ats_issues_col, 1)
        cols.addWidget(self.ats_sugg_col, 1)
        body.addLayout(cols, 4)
        root.addLayout(body)

        self.ats_keyword_host = FlowWidget()
        root.addWidget(self.ats_keyword_host)
        root.addWidget(hint_label(
            "绿=已命中 · 黄=措辞不同已对齐 · 红=原文缺失（缺的请补真实经历，不要硬塞）"))

        self.ats_card.setVisible(False)
        return self.ats_card

    @staticmethod
    def _ats_list_col(title: str) -> tuple:
        """一列结论（列标题 + 待填充的竖排布局）。"""
        host = QWidget()
        # 纯布局容器：不声明透明就会在卡片里露出一块页面底色（见 theme 的 flush）
        host.setProperty("class", "flush")
        col = QVBoxLayout(host)
        col.setContentsMargins(*tokens.ZERO_MARGINS)
        col.setSpacing(tokens.SP_XS)
        col.addWidget(hint_label(title))
        return host, col

    @staticmethod
    def _clear_ats_list(layout) -> None:
        """清空结论列，**保留第 0 项的列标题**。"""
        while layout.count() > 1:
            item = layout.takeAt(1)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _fill_ats_list(self, layout, items, empty_text: str, tone_class: str) -> None:
        self._clear_ats_list(layout)
        if not items:
            layout.addWidget(hint_label(empty_text))
            return
        for text in items:
            label = QLabel(f"· {text}")
            label.setWordWrap(True)
            # 用语义 class 而不是内联 color：内联色切深色主题时不会重刷，
            # 会变成深底上的暗红/暗绿（status_chip 踩过同样的坑）。
            set_prop(label, "class", tone_class)
            layout.addWidget(label)

    def _fill_ats_keywords(self, matched, partial, missing) -> None:
        self.ats_keyword_host.clear()
        rows = [("matched", matched), ("partial", partial), ("missing", missing)]
        total = 0
        for kind, words in rows:
            for word in words:
                total += 1
                self.ats_keyword_host.add(soft_chip(str(word), _hit_tone(kind)))
        if not total:
            self.ats_keyword_host.add(hint_label("本次没有可对比的关键词"))

    def _render_ats_delta(self, delta: int) -> None:
        if delta > 0:
            cls, text = "text-success", f"↑ 提升 {delta} 分"
        elif delta < 0:
            cls, text = "text-danger", f"↓ 下降 {abs(delta)} 分"
        else:
            cls, text = "hint", "分数持平"
        self.ats_delta_label.setText(text)
        set_prop(self.ats_delta_label, "class", cls)

    def _render_ats(self, ats, ats_after, gap) -> None:
        """铺「优化前 → 优化后」的 ATS 对比。

        任一份缺失就**整块收起**：那是「服务端这次没返回 ATS」，跟
        「返回了 0 分」是两回事（见 ``api_client._norm_ats``）。照着
        normalizeResult 的 ``?? {score: 0}`` 补 0 会把「没有数据」显示成
        「考了 0 分」，属于伪造。
        """
        gap = gap or {}
        if not ats or not ats_after:
            self.ats_card.setVisible(False)
            return
        before, after = int(ats["score"]), int(ats_after["score"])
        self.ats_card.setVisible(True)
        self.ats_chart.set_scores(before, after,
                                  before_color=tokens.C.WARN,
                                  after_color=tokens.C.SUCCESS)
        self._render_ats_delta(after - before)

        matched = list(gap.get("matched") or [])
        partial = list(gap.get("partial") or [])
        missing = list(gap.get("missing") or [])
        self.ats_hit_label.setText(
            f"关键词：命中 {len(matched)} · 对齐 {len(partial)} · 缺失 {len(missing)}")
        self._fill_ats_list(self.ats_issues_layout, ats.get("issues") or [],
                            "没有发现结构性问题", "text-warn")
        self._fill_ats_list(self.ats_suggestions_layout,
                            ats_after.get("suggestions") or [],
                            "没有待改进项", "text-success")
        self._fill_ats_keywords(matched, partial, missing)

    def _clear_ats(self) -> None:
        """收起上一轮对比（重跑/失败时调用），避免旧分数挂在屏幕上。"""
        self.ats_card.setVisible(False)
        self.ats_chart.set_scores(None, None)
        self.ats_delta_label.setText("")
        self.ats_hit_label.setText("")
        self.ats_keyword_host.clear()
        self._clear_ats_list(self.ats_issues_layout)
        self._clear_ats_list(self.ats_suggestions_layout)

    # ===== 历史版本 =====
    @staticmethod
    def _fmt_time(raw: str) -> str:
        """ISO 时间串 → 2026-09-20 10:59（解析失败就原样返回）。"""
        text = str(raw or "")
        if "T" in text:
            date_part, _, time_part = text.partition("T")
            return f"{date_part} {time_part[:5]}"
        return text[:16] or "—"

    # ---------- 质检（钉到原文行） ----------

    def _clear_review(self) -> None:
        while self.review_layout.count():
            item = self.review_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render_review(self, review: dict) -> None:
        """把质检结论铺成一行一条，带行号的可一键跳转。"""
        self._clear_review()
        issues = list((review or {}).get("issues") or [])
        anchored = sum(1 for i in issues if i.get("line"))
        score = (review or {}).get("score")
        if score is None:
            self.review_scope_label.setText("优化完成后，这里会列出内容质检结论。")
        else:
            self.review_scope_label.setText(
                f"内容质量 {score} 分（{review.get('level', '')}）· "
                f"共 {len(issues)} 条，其中 {anchored} 条能定位到具体行")
        if not issues:
            if score is not None:
                self.review_layout.addWidget(
                    hint_label("未发现撰写规范问题，可直接导出投递。"))
            self.review_layout.addStretch(1)
            return
        for issue in issues:
            self.review_layout.addWidget(self._review_row(issue))
        self.review_layout.addStretch(1)

    def _review_row(self, issue: dict) -> QFrame:
        tone = {"high": "#dc2626", "medium": "#d97706"}.get(
            issue.get("severity"), "#6b7280")
        frame = card()
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)

        col = QVBoxLayout()
        col.setSpacing(3)
        loc = issue.get("location") or ""
        line = issue.get("line")
        prefix = f"第 {line} 行 · " if line else ""
        head = QLabel(f"{prefix}{loc}：{issue.get('message', '')}")
        head.setWordWrap(True)
        head.setStyleSheet(f"color:{tone};")
        col.addWidget(head)
        if issue.get("snippet"):
            snip = hint_label("原文：" + str(issue["snippet"]))
            snip.setWordWrap(True)
            col.addWidget(snip)
        if issue.get("fix"):
            fix = hint_label("→ " + str(issue["fix"]))
            fix.setWordWrap(True)
            col.addWidget(fix)
        row.addLayout(col, 1)

        if line:
            btn = QPushButton(f"跳到第 {line} 行")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _checked=False, n=int(line): self._jump_to_line(n))
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        return frame

    def _jump_to_line(self, line: int) -> None:
        """把「优化后」视图的光标移到第 line 行并选中整行，同时切到对比页。

        定位用 block 而不是字符偏移：QTextEdit 里一个 block 就是一行，
        比自己去数换行符稳（不同来源的粘贴内容换行符不一致）。
        切页是必须的 —— 在一个看不见的 Tab 里移动光标等于什么都没发生。
        """
        doc = self.after_view.document()
        block = doc.findBlockByNumber(int(line) - 1)
        if not block.isValid():
            return
        self.tabs.setCurrentIndex(0)
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        self.after_view.setTextCursor(cursor)
        self.after_view.ensureCursorVisible()
        self.after_view.setFocus()

    def _clear_versions(self) -> None:
        while self.versions_layout.count():
            item = self.versions_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _load_versions(self, *_args) -> None:
        """拉取当前选中简历的历史版本（网络请求放后台线程）。"""
        resume_id = self.resume_combo.currentData()
        self._clear_versions()
        if not resume_id:
            self.versions_layout.addWidget(hint_label("先选择一份简历，这里会列出它的历史版本。"))
            self.versions_layout.addStretch(1)
            return
        self.versions_layout.addWidget(hint_label("读取中…"))
        self.versions_layout.addStretch(1)
        self.versions_refresh_btn.setEnabled(False)
        worker = LLMWorker(api_guard(lambda: db.list_resume_versions(resume_id)))
        worker.success.connect(self._render_versions)
        worker.failed.connect(self._on_versions_failed)
        worker.start()
        self.versions_worker = worker

    def _on_versions_failed(self, msg: str) -> None:
        self.versions_refresh_btn.setEnabled(True)
        self._clear_versions()
        self.versions_layout.addWidget(hint_label(f"读取历史版本失败：{msg}"))
        self.versions_layout.addStretch(1)

    def _render_versions(self, versions) -> None:
        self.versions_refresh_btn.setEnabled(True)
        self._clear_versions()
        versions = list(versions or [])
        if not versions:
            self.versions_layout.addWidget(hint_label("这份简历还没有历史版本。"))
            self.versions_layout.addStretch(1)
            return
        current_version = max((v.get("version") or 0) for v in versions)
        for v in versions:
            self.versions_layout.addWidget(self._version_row(v, current_version))
        self.versions_layout.addStretch(1)

    def _version_row(self, v: dict, current_version: int) -> QFrame:
        ver = int(v.get("version") or 0)
        frame = card(highlight=(ver == current_version))
        row = QHBoxLayout(frame)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(4)
        title = QLabel(f"版本 v{ver}" + ("（当前）" if ver == current_version else ""))
        title.setStyleSheet("font-size: 13px; font-weight: 700;")
        raw = str(v.get("raw_text") or "")
        source = str(v.get("source_file") or "").strip() or "手动保存 / AI 生成"
        sub = hint_label(f"{self._fmt_time(v.get('created_at'))} · {len(raw)} 字 · {source}")
        # 单行信息不要开 wordWrap：换行会让卡片高度在布局过程中抖动
        sub.setWordWrap(False)
        info.addWidget(title)
        info.addWidget(sub)
        row.addLayout(info, 1)

        if ver != current_version:
            btn = QPushButton("恢复到此版本")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _checked=False, vid=v.get("id"), ver_=ver: self._rollback(vid, ver_)
            )
            row.addWidget(btn)
        else:
            row.addWidget(hint_label("当前使用中"))
        return frame

    def _rollback(self, version_id, version: int) -> None:
        resume_id = self.resume_combo.currentData()
        if not resume_id or not version_id:
            return
        ret = QMessageBox.question(
            self, "恢复历史版本",
            f"确定把这份简历恢复到 v{version} 吗？\n\n"
            "当前内容会被该版本覆盖（原内容仍保留在版本历史里，可以再恢复回来）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.overlay.show_over(f"正在恢复到 v{version}…")
        worker = LLMWorker(api_guard(
            lambda: db.rollback_resume_version(resume_id, version_id)))
        worker.success.connect(lambda _r: self._on_rolled_back(version))
        worker.failed.connect(self._on_rollback_failed)
        worker.start()
        self._rollback_worker = worker

    def _on_rolled_back(self, version: int) -> None:
        self.overlay.hide()
        self._refresh()
        QMessageBox.information(self, "已恢复", f"简历已恢复到 v{version}。")

    def _on_rollback_failed(self, msg: str) -> None:
        self.overlay.hide()
        QMessageBox.warning(self, "恢复失败", str(msg))

    def _open_agent(self) -> None:
        """从简历优化页带着当前简历+岗位进入求职 Agent（审计 P1-2）。"""
        if not self._resume or not self._jd:
            QMessageBox.information(self, "提示", "请先选择「简历」与「目标岗位」")
            return
        self.mw.open_agent({
            "resume_id": self._resume["id"],
            "jd_id": self._jd["id"],
            "entry_source": "resume_optimize",
        })

    def _optimize(self) -> None:
        resume_id = self.resume_combo.currentData()
        jd_id = self.jd_combo.currentData()
        if not resume_id or not jd_id:
            QMessageBox.warning(self, "缺少选择", "请先选择要优化的简历和目标岗位")
            return
        self._resume = db.get_resume(resume_id)
        self._jd = db.get_jd(jd_id)
        if not self._resume or not self._jd:
            return
        self.optimize_btn.setEnabled(False)
        self._clear_ats()
        # 优化已本地化：三道重试（整份重写 / 三刀裁剪 / 保真回退）都会让耗时波动，
        # 所以不写死秒数。
        self.overlay.show_over("AI 正在做差距分析并整份重写（本机运行）…")

        def do():
            # 本地优先；只有「本地模型压根没调起来」（llm_failed）才回落云端。
            # 校验不通过**不**回落 —— 云端跑的是同一套判据，回落只是白等一次网络。
            result = ro.optimize_and_save(self._resume, self._jd)
            if result.get("llm_failed"):
                return db.optimize_resume(resume_id, jd_id)
            return result

        self._worker = LLMWorker(do)
        self._worker.success.connect(self._on_optimized)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_failed(self, msg: str) -> None:
        self.overlay.hide()
        # 走统一入口而不是写死 True：按钮的可用性由「有没有简历」决定，
        # 这里硬开会在极端情况下（列表被清空）把一个空选择留在可点状态。
        self._sync_action_state()
        # 失败时也要清：上一轮的 ATS 分数留在屏幕上，会被当成这次的结论
        self._clear_ats()
        QMessageBox.warning(self, "优化失败", msg)

    def _on_optimized(self, result: dict) -> None:
        self.overlay.hide()
        self._sync_action_state()
        self._result = result

        self.before_view.setPlainText(self._resume["raw_text"])
        self.after_view.setPlainText(result.get("optimized_text", ""))

        # 变更明细
        lines = []
        if result.get("error"):
            lines.append("⚠ " + result["error"] + "\n")
        gap = result.get("gap") or {}
        if gap.get("matched"):
            lines.append("已命中 JD 要求：" + "、".join(gap["matched"]))
        if gap.get("missing"):
            lines.append("缺失（请勿硬塞，建议补经历）：" + "、".join(gap["missing"]))
        if gap.get("partial"):
            lines.append("相关但需强化（措辞没对齐 JD）：" + "、".join(gap["partial"]))
        if result.get("added_keywords"):
            lines.append("本次新增对齐关键词：" + "、".join(result["added_keywords"]))
        # 召回轨迹：优化同样「先查库再生成」，把「查到了什么」一并摆出来
        retr = retrieval_summary(result.get("retrieval"))
        if retr:
            lines.append(retr)
        pres = result.get("preservation") or {}
        lines.append(f"事实保真校验：{'通过' if pres.get('passed') else '未通过'}（保留度 {_pct(pres.get('score'))}）")
        if result.get("length_warning"):
            lines.append("篇幅提示：" + result["length_warning"])
        if result.get("removed"):
            lines.append("已精简内容：" + "；".join(result["removed"]))

        # 内容质量质检（12 条撰写规范，对标成熟简历产品）
        from ...core.resume_review import review_text
        review = review_text(result.get("optimized_text", ""),
                             (self._jd or {}).get("raw_text") or "")
        lines.append(f"内容质量：{review['score']} 分（{review['level']}）")
        for good in review.get("highlights", [])[:3]:
            lines.append("  ✓ " + good)
        for issue in review.get("issues", [])[:8]:
            where = f"第 {issue['line']} 行·{issue['location']}" if issue.get("line") \
                else issue["location"]
            lines.append(f"  · [{issue['severity']}] {where}：{issue['message']}")
            if issue.get("fix"):
                lines.append(f"      → {issue['fix']}")
        if len(review.get("issues", [])) > 8:
            lines.append(f"  （另有 {len(review['issues']) - 8} 条改进项）")

        # 本地守门层：与生成链路口径一致（``core/generation_guard``）。
        # 优化接口只回文本、没有 structured，所以先解析成块再查；事实来源是
        # **用户手上那份原文**，不是优化稿本身 —— 拿优化稿自证必然全过，
        # 那正是这条链路此前缺的把关（整份重写后没有任何本地编造检查）。
        guard = rg.guard_text(
            result.get("optimized_text", ""),
            (self._resume or {}).get("raw_text") or "",
            (self._jd or {}).get("raw_text") or "",
        )
        lines.append(f"编造/风险守门：{guard['score']} 分"
                     f"（{'通过' if guard['passed'] else '未通过'}）")
        for item in guard.get("issues", [])[:6]:
            lines.append(f"  · [{item['severity']}] {item['location']}：{item['message']}")
            if item.get("fix"):
                lines.append(f"      → {item['fix']}")
        if len(guard.get("issues", [])) > 6:
            lines.append(f"  （另有 {len(guard['issues']) - 6} 条守门提示）")

        lines.append("")
        for i, change in enumerate(result.get("changes", []), 1):
            lines.append(f"{i}. 【{change['section']}】")
            lines.append(f"   原：{change['before'][:80]}")
            lines.append(f"   改：{change['after'][:80]}")
            lines.append(f"   因：{change['reason']}")
            lines.append("")
        self.changes_view.setPlainText("\n".join(lines))
        # 质检 Tab 与「变更明细」同时刷新：两处讲的是同一份结论，
        # 只刷一处就会出现"明细里有、质检里没有"的假不一致。
        self._render_review(review)
        # ATS 前后对比：分数来自服务端（同一条规则跑原文与优化稿），
        # 与上面的本地「内容质检」是两套东西 —— 质检看撰写规范，ATS 看
        # 机器可读性（联系方式/分节/长度/JD 关键词覆盖），互不替代。
        self._render_ats(result.get("ats"), result.get("ats_after"),
                         result.get("gap"))

        ok = bool(result.get("optimized_text")) and not result.get("error")
        # 保存落点在本地（`local_store`），联网后由同步线程自动上行；回落云端时
        # 由云端 `/optimize` 自己建新版本。两种情况都靠 `new_version` 判「有没有落库」,
        # 不要按来源写死。
        # ⚠️ 只有真的成功才说"已保存" —— 失败还这么写会与上面的
        # 「❌ 已回退原文」自相矛盾，用户会以为改动已经落库。
        if not ok:
            self.save_btn.setText("未保存（本次优化失败）")
        elif result.get("new_version"):
            self.save_btn.setText("已保存为新版本")
        else:
            self.save_btn.setText("未落库（未登录或本地存储不可用）")
        self.save_btn.setEnabled(False)
        self.export_btn.setEnabled(ok)
        self.export_txt_btn.setEnabled(ok)
        ats, ats_after = result.get("ats"), result.get("ats_after")
        ats_text = f" · ATS {ats['score']} → {ats_after['score']}" if (ats and ats_after) else ""
        status = "✅ 校验通过" if pres.get("passed") else "❌ 已回退原文"
        if not ok:
            status = "❌ 优化失败"
        self.status_label.setText(
            f"编辑 {result.get('edit_count', 0)} 处 · 内容质量 {review['score']} 分 · "
            f"保留度 {_pct(pres.get('score'))}{ats_text} · " + status
        )
        # 优化稿是**新记录**（本地落库，不是同文档新版本）⇒ 不刷新下拉的话，
        # 用户「明明说已保存」却在简历列表里找不到它。`_refresh` 会保留当前
        # 选中的那份，所以这里不会把用户的选择跳走。
        if ok and result.get("new_version"):
            self._refresh()

    def _save(self) -> None:
        if not self._result:
            return
        QMessageBox.information(
            self, "已保存",
            "优化后的简历已保存为新版本（存在本机，联网后自动同步），"
            "原版本仍在列表中可随时切回。"
        )

    def _export_with_picker(self) -> None:
        """先选版式（右侧直接看真实 PDF 预览），再导出 Word 或 PDF。"""
        blocks = self._export_blocks()
        if not blocks:
            return
        dlg = TemplatePickerDialog(blocks, self, default_key=self._template_key,
                                   from_blocks=True,
                                   default_appearance=self._appearance,
                                   default_order=self._order)
        code = dlg.exec()
        if code in (TemplatePickerDialog.RESULT_DOCX,
                    TemplatePickerDialog.RESULT_PDF):
            self._template_key = dlg.selected_key
            self._appearance = dlg.selected_appearance
            self._order = dlg.selected_order
            save_export_options(self._appearance, self._order)
            self._export("docx" if code == TemplatePickerDialog.RESULT_DOCX else "pdf")

    def _export_note(self) -> str:
        """导出成功提示里附上「用了哪套版式 + 外观 + 顺序」，只列非默认项。"""
        look = self._appearance
        parts = [f"版式：{templates.get_template(self._template_key).label}"]
        if look.accent_hex:
            parts.append(f"强调色 #{look.accent_hex}")
        if abs(look.font_scale - 1.0) > 1e-6:
            parts.append(f"字号 {int(round(look.font_scale * 100))}%")
        if list(self._order) != list(BODY_SECTIONS):
            parts.append("顺序：" + " → ".join(section_labels_in_order(self._order)))
        return "\n\n" + "　|　".join(parts)

    def _export_blocks(self):
        """把优化后的纯文本解析成结构化块（导出与预览共用）。"""
        if not self._result:
            return None
        blocks = parse_resume_text(self._result["optimized_text"])
        name = ((self._resume.get("parsed_json") or {}).get("name")) or "简历"
        if not blocks.get("name"):
            blocks["name"] = name
        return blocks

    def _export(self, fmt: str) -> None:
        # 占位符拦截放在选路径之前：让用户先补数字，而不是选完路径才被弹窗打断
        if not confirm_placeholders(self, (self._result or {}).get("optimized_text", "")):
            return
        blocks = self._export_blocks()
        if not blocks:
            return
        name = blocks.get("name") or "简历"
        default = os.path.join(local_db.resolve_export_dir(), f"{name}_优化版.{fmt}")
        path, _ = QFileDialog.getSaveFileName(self, "导出简历", default, f"文件 (*.{fmt})")
        if not path:
            return
        try:
            if fmt == "docx":
                export_docx_from_blocks(blocks, path, self._template_key,
                                        order=self._order,
                                        appearance=self._appearance)
            elif fmt == "pdf":
                export_pdf_from_blocks(blocks, path, self._template_key,
                                       order=self._order,
                                       appearance=self._appearance)
            else:
                export_txt(self._result["optimized_text"], path)
            extra = ""
            if fmt in ("docx", "pdf"):
                extra = self._export_note()
            # 只有 PDF 值得回读：docx 是 XML，字在不在肉眼可查；
            # PDF 要过中文字体子集化 + 一页调优两道"静默丢字"的关。
            verify = self._verify_pdf(path, blocks) if fmt == "pdf" else None
            notify_export_result(self, path, extra, verify)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _verify_pdf(self, path: str, blocks) -> "pdf_verify.PdfVerifyResult | None":
        """导出后回读自检。

        自检本身出任何问题都返回 None（界面只说「未做回读校验」），
        **绝不**让它把一次成功的导出报成失败 —— 用户点的是导出。
        """
        try:
            return pdf_verify.verify_pdf_file(path, blocks)
        except Exception:
            return None
