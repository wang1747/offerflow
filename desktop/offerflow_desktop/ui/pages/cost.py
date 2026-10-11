"""AI 用量与成本：累计费用 / Token / 调用次数 + 按功能与模型分布 + 调用明细节。

对齐网页版 CostDashboard.tsx，走同一组接口：
    GET /api/v1/observability/summary       （汇总 + 按操作/模型聚合）
    GET /api/v1/observability/traces        （调用明细，注意是裸数组）
    GET /api/v1/observability/traces/{id}   （单次调用的 span 明细，点开才拉）

「最近调用记录」不再是平铺表格，而是**可展开的下钻列表**：点一行才去拉这条
记录的 span 明细，展开区呈现调用链瀑布图 + 模型/输入/输出/耗时/费用表。
"""

from typing import Any, Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ... import api_client as api
from ...llm import LLMWorker
from ..charts import WaterfallChart
from ..components import (api_guard, card, hint_label, make_title, repolish,
                          section_label, stat_card)

# 与网页版 CostDashboard.tsx 的 OPERATION_LABELS / OPERATION_COLORS 保持一致
OPERATION_LABELS: Dict[str, str] = {
    "jd_parse": "JD 解析",
    "resume_optimize": "简历优化",
    "interview_prep": "面试题生成",
    "mock_interview_summary": "面试总结",
    "mock_interview_question": "面试提问",
    "mock_interview_feedback": "面试反馈",
    "article_extract": "面经解析",
    "resume_generate": "简历生成",
    "resume_generate_retry": "简历生成重试",
    "resume_condense": "简历精简",
    "resume_generate_condense": "简历生成精简",
    "resume_optimize_extract": "简历优化提取",
    "resume_regenerate_section": "简历逐段重写",
    "resume_regenerate_variants": "简历改写候选",
    "salary_reference_extract": "薪资画像提取",
    "salary_reference_analysis": "薪资解读",
    "negotiation_hr": "谈判 HR",
    "negotiation_coach": "谈判教练",
    "negotiation_summary": "谈判总结",
    "salary_benchmark_refresh": "薪资基准刷新",
}

OPERATION_COLORS: Dict[str, str] = {
    "jd_parse": "#4f46e5",
    "resume_optimize": "#8b5cf6",
    "interview_prep": "#06b6d4",
    "mock_interview_summary": "#14b8a6",
    "mock_interview_question": "#3b82f6",
    "mock_interview_feedback": "#f59e0b",
    "article_extract": "#f97316",
    "resume_generate": "#6366f1",
    "resume_generate_retry": "#818cf8",
    "resume_condense": "#a78bfa",
    "resume_generate_condense": "#c084fc",
    "resume_optimize_extract": "#d946ef",
    "resume_regenerate_section": "#ec4899",
    "resume_regenerate_variants": "#f43f5e",
    "salary_reference_extract": "#0ea5e9",
    "salary_reference_analysis": "#10b981",
    "negotiation_hr": "#eab308",
    "negotiation_coach": "#ef4444",
    "negotiation_summary": "#64748b",
    "salary_benchmark_refresh": "#22c55e",
}

MODEL_COLORS = ["#4f46e5", "#8b5cf6", "#06b6d4", "#14b8a6", "#f59e0b", "#ef4444", "#3b82f6"]


def op_label(key: str) -> str:
    """操作英文名 → 中文标签（没映射的原样显示）。"""
    return OPERATION_LABELS.get(key, key or "-")


def fmt_cost(v: float) -> str:
    """人民币格式化：不足 1 元保留 4 位小数，否则保留 2 位。

    成本底层以美元计价（对齐模型厂商账单），后端已按参考汇率折算成人民币返回。
    """
    if not v:
        return "¥0.00"
    return f"¥{v:.4f}" if v < 1 else f"¥{v:.2f}"


def fmt_tokens(v: int) -> str:
    return f"{v:,}"


def fmt_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60000}m{round(ms % 60000 / 1000)}s"


def fmt_time(raw: str) -> str:
    """2026-09-23T01:16:11.105235 → 09-23 09:16（后端存的是 UTC，+8 转北京时间）。"""
    text = str(raw or "")
    if "T" not in text:
        return text[:16] or "—"
    try:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(text)
        return (dt + timedelta(hours=8)).strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16]


def span_rows(payload: Dict[str, Any]) -> List[List[str]]:
    """span 明细 → 表格单元格文本（模型 / 输入 / 输出 / 耗时 / 费用）。

    独立成纯函数是为了让判据能直接断言数值与「未知」口径，不必建窗口、
    也不依赖 QTableWidget 的取值方式。

    `cost_cny` 为 ``None`` 时显示「未知」而**不是** ¥0.00 —— 后者会让用户
    以为这次调用不花钱（后端查不到该模型价格时才给 null）。
    """
    rows: List[List[str]] = []
    for s in (payload or {}).get("spans") or []:
        if not isinstance(s, dict):
            continue
        cost = s.get("cost_cny")
        rows.append([
            str(s.get("model") or "未知"),
            fmt_tokens(int(s.get("prompt_tokens") or 0)),
            fmt_tokens(int(s.get("completion_tokens") or 0)),
            fmt_duration(int(s.get("duration_ms") or 0)),
            "未知" if cost is None else fmt_cost(float(cost)),
        ])
    return rows


class TraceRow(QFrame):
    """一条调用记录：头部一行（点击 / 回车展开）+ 展开区（**延迟构造**）。

    为什么不用 QTableWidget 做下钻：表格单元格塞不进「瀑布图 + 明细表」这类
    富内容；而且展开区必须延迟构造 —— 一次最多 50 条，构造期全建一遍在滚动
    时会明显发涩。
    """

    #: 点行 → 参数是 trace id。真正展开由页面做（它要先收起上一条、再拉明细）。
    toggled = Signal(str)

    def __init__(self, trace: Dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.trace = dict(trace or {})
        self._open = False
        self.waterfall: WaterfallChart | None = None
        self.span_table: QTableWidget | None = None

        self.setObjectName("TraceRow")
        self.setProperty("open", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        status = str(self.trace.get("status") or "success")
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(
            "font-size: 11px; color: %s;"
            % ("#ef4444" if status != "success" else "#10b981"))
        head.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.operation_label = QLabel(
            op_label(str(self.trace.get("operation") or "")))
        self.operation_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.created_label = hint_label(fmt_time(self.trace.get("created_at")))
        col.addWidget(self.operation_label)
        col.addWidget(self.created_label)
        head.addLayout(col, 1)

        self.tokens_label = hint_label(
            f"{fmt_tokens(int(self.trace.get('total_tokens') or 0))} tok")
        head.addWidget(self.tokens_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.duration_label = hint_label(
            fmt_duration(int(self.trace.get("duration_ms") or 0)))
        head.addWidget(self.duration_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.cost_label = QLabel(
            fmt_cost(float(self.trace.get("total_cost_cny") or 0)))
        self.cost_label.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #4f46e5;")
        head.addWidget(self.cost_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.arrow_label = QLabel("▾")
        self.arrow_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        head.addWidget(self.arrow_label, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(head)

        self.detail = QWidget()
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setSpacing(6)
        self.detail.setVisible(False)
        outer.addWidget(self.detail)

    # ===== 状态 =====

    @property
    def trace_id(self) -> str:
        return str(self.trace.get("id") or "")

    @property
    def is_open(self) -> bool:
        return self._open

    def set_open(self, is_open: bool) -> None:
        """只负责展开/收起的**视觉状态**，内容由 set_detail / set_message 填。"""
        self._open = bool(is_open)
        self.setProperty("open", "true" if self._open else "false")
        self.arrow_label.setText("▴" if self._open else "▾")
        self.detail.setVisible(self._open)
        repolish(self)

    def toggle(self) -> None:
        self.toggled.emit(self.trace_id)

    # ===== 展开区内容 =====

    def clear_detail(self) -> None:
        self.waterfall = None
        self.span_table = None
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def set_message(self, text: str) -> None:
        """展开区放一条纯文字（读取中 / 读取失败 / 没有明细）。"""
        self.clear_detail()
        self.detail_layout.addWidget(hint_label(text))

    def set_detail(self, payload: Dict[str, Any]) -> None:
        """展开区内容：调用链瀑布图 + span 明细表。"""
        self.clear_detail()
        spans = [s for s in ((payload or {}).get("spans") or [])
                 if isinstance(s, dict)]
        if not spans:
            self.detail_layout.addWidget(hint_label("这条记录没有明细 span。"))
            return
        trace = (payload or {}).get("trace") or {}
        total = trace.get("duration_ms")
        if total is None:
            total = self.trace.get("duration_ms") or 0

        self.detail_layout.addWidget(section_label("调用链时间线"))
        self.waterfall = WaterfallChart()
        self.waterfall.set_spans(spans, total)
        self.detail_layout.addWidget(self.waterfall)

        rows = span_rows(payload)
        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels(["模型", "输入", "输出", "耗时", "费用"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = table.horizontalHeader()
        for i in range(1, 5):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                table.setItem(r, c, QTableWidgetItem(text))
        table.setFixedHeight(min(260, 30 + len(rows) * 26))
        self.span_table = table
        self.detail_layout.addWidget(table)

    # ===== 交互（写法对齐 ui/nav_list.py 的 NavRow）=====

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 用 release + 命中判断：按下后拖出去再松开应当取消
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.toggle()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.toggle()
            return
        super().keyPressEvent(event)


class CostPage(QWidget):
    """AI 用量与成本。"""

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None
        self._loading = False
        #: trace id → 行控件（回调按 id 查，绝不长期持有行对象：刷新会销毁它们）
        self._rows: Dict[str, TraceRow] = {}
        #: 当前展开的那一条（手风琴：同时只开一条）
        self._expanded_id: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        head = QHBoxLayout()
        head.addWidget(make_title(
            "AI 用量与成本",
            "这个账号的模型调用消耗明细。未配置自有 Key 时使用平台默认模型，用量按平台价格折算。",
        ), 1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._load)
        head.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(head)

        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(12)
        layout.addLayout(self.cards_row)

        self.by_op_host = QVBoxLayout()
        self.by_op_host.setSpacing(4)
        op_card = card()
        op_l = QVBoxLayout(op_card)
        op_l.setContentsMargins(16, 14, 16, 14)
        op_l.setSpacing(8)
        op_l.addWidget(section_label("按功能分布"))
        op_l.addWidget(hint_label("各功能花的钱，条越长占比越高。"))
        op_l.addLayout(self.by_op_host)
        layout.addWidget(op_card)

        self.by_model_host = QVBoxLayout()
        self.by_model_host.setSpacing(4)
        model_card = card()
        ml = QVBoxLayout(model_card)
        ml.setContentsMargins(16, 14, 16, 14)
        ml.setSpacing(8)
        ml.addWidget(section_label("按模型占比"))
        ml.addLayout(self.by_model_host)
        layout.addWidget(model_card)

        self.trace_card = card()
        tl = QVBoxLayout(self.trace_card)
        tl.setContentsMargins(16, 14, 16, 14)
        tl.setSpacing(8)
        tl.addWidget(section_label("最近调用记录"))
        tl.addWidget(hint_label(
            "点一行展开这次调用的链路明细：每个模型调用的起止时刻、耗时与费用"
            "逐条列出（最多显示最近 50 条）。"))
        self.trace_host = QVBoxLayout()
        self.trace_host.setSpacing(6)
        tl.addLayout(self.trace_host)

        # 空状态：有数据时隐藏
        self.trace_empty = QLabel("暂无调用记录")
        self.trace_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trace_empty.setProperty("class", "hint")
        self.trace_empty.setWordWrap(True)
        self.trace_empty.setMinimumHeight(120)
        self.trace_empty.setStyleSheet("color: #64748b; padding: 36px 0;")
        tl.addWidget(self.trace_empty)

        layout.addWidget(self.trace_card)

        self.status_label = hint_label("")
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ==================== 数据 ====================

    def _load(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("读取中…")

        def do() -> Dict[str, Any]:
            return {
                "summary": api.get_cost_summary(),
                "traces": api.list_cost_traces(page=1, page_size=50),
            }

        worker = LLMWorker(api_guard(do))
        worker.success.connect(self._render)
        worker.failed.connect(self._failed)
        worker.start()
        self._worker = worker

    def _failed(self, msg: str) -> None:
        self._loading = False
        self.refresh_btn.setEnabled(True)
        self.status_label.setText(f"读取失败：{msg}")

    def _clear(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render(self, payload: Dict[str, Any]) -> None:
        self._loading = False
        self.refresh_btn.setEnabled(True)
        summary = payload.get("summary") or {}
        traces = (payload.get("traces") or {}).get("items") or []

        # 统计卡
        self._clear(self.cards_row)
        self.cards_row.addWidget(stat_card(
            "累计费用", fmt_cost(float(summary.get("total_cost_cny") or 0)),
            accent="#4f46e5", sub="按参考汇率折算"))
        self.cards_row.addWidget(stat_card(
            "累计 Token", fmt_tokens(int(summary.get("total_tokens") or 0)),
            accent="#8b5cf6", sub="输入 + 输出合计"))
        self.cards_row.addWidget(stat_card(
            "调用次数", str(int(summary.get("trace_count") or 0)),
            accent="#06b6d4", sub="含失败调用"))

        self._render_dist(self.by_op_host, summary.get("by_operation") or [], "功能")
        self._render_model(self.by_model_host, summary.get("by_model") or [])
        self._render_traces(traces)

        total_cost = float(summary.get("total_cost_cny") or 0)
        if not total_cost and not traces:
            self.status_label.setText("还没有成本数据，做一次 AI 操作后回来看看。")
        else:
            self.status_label.setText(
                f"共 {int(summary.get('trace_count') or 0)} 次调用 · "
                f"{fmt_tokens(int(summary.get('total_tokens') or 0))} token · "
                f"{fmt_cost(total_cost)}")

    def _render_dist(self, layout, rows: List[Dict[str, Any]], kind: str) -> None:
        self._clear(layout)
        if not rows:
            layout.addWidget(hint_label("暂无分布数据。"))
            return
        total = sum(float(r.get("cost_cny") or 0) for r in rows) or 1.0
        for r in rows:
            key = str(r.get("key") or "-")
            cost = float(r.get("cost_cny") or 0)
            pct = cost / total * 100 if total else 0
            color = OPERATION_COLORS.get(key, "#6366f1")

            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(4)
            head = QHBoxLayout()
            name = QLabel(op_label(key) if kind == "功能" else key)
            name.setStyleSheet("font-size: 13px; font-weight: 600;")
            head.addWidget(name)
            head.addStretch(1)
            meta = hint_label(
                f"{fmt_cost(cost)} · {fmt_tokens(int(r.get('tokens') or 0))} token · "
                f"{int(r.get('count') or 0)} 次 · {pct:.0f}%")
            # 固定宽度 + 右对齐：各条数值成一列，否则会随条长左右浮动、还会折行
            meta.setWordWrap(False)
            meta.setFixedWidth(210)
            meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            head.addWidget(meta)
            rl.addLayout(head)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(max(1, int(round(pct))))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(
                "QProgressBar { background: rgba(148,163,184,0.20); border: none;"
                " border-radius: 3px; }"
                f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")
            rl.addWidget(bar)
            layout.addWidget(row)

    def _render_model(self, layout, rows: List[Dict[str, Any]]) -> None:
        self._clear(layout)
        if not rows:
            layout.addWidget(hint_label("暂无模型数据。"))
            return
        total = sum(float(r.get("cost_cny") or 0) for r in rows) or 1.0
        for i, r in enumerate(rows):
            name_text = str(r.get("key") or "-")
            cost = float(r.get("cost_cny") or 0)
            pct = cost / total * 100 if total else 0
            color = MODEL_COLORS[i % len(MODEL_COLORS)]
            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(4)
            head = QHBoxLayout()
            name = QLabel(name_text)
            name.setStyleSheet("font-size: 13px; font-weight: 600;")
            head.addWidget(name)
            head.addStretch(1)
            meta = hint_label(
                f"{fmt_cost(cost)} · {fmt_tokens(int(r.get('tokens') or 0))} token · "
                f"{pct:.0f}%")
            meta.setWordWrap(False)
            meta.setFixedWidth(210)
            meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            head.addWidget(meta)
            rl.addLayout(head)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(max(1, int(round(pct))))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(
                "QProgressBar { background: rgba(148,163,184,0.20); border: none;"
                " border-radius: 3px; }"
                f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")
            rl.addWidget(bar)
            layout.addWidget(row)

    def _render_traces(self, traces: List[Dict[str, Any]]) -> None:
        self._clear(self.trace_host)
        self._rows = {}
        self._expanded_id = None
        if not traces:
            self.trace_empty.setText(
                "还没有 AI 调用记录。\n做一次简历生成、岗位分析或面试准备等操作后，这里会显示明细。")
            self.trace_empty.setVisible(True)
            return
        self.trace_empty.setVisible(False)
        for t in traces:
            row = TraceRow(t)
            row.toggled.connect(self._on_trace_toggle)
            self.trace_host.addWidget(row)
            if row.trace_id:
                self._rows[row.trace_id] = row

    def _on_trace_toggle(self, trace_id: str) -> None:
        """点开/收起一条调用记录 —— **展开时才去拉**这条记录的 span 明细。

        回调只捕获 ``trace_id``、进来再查 ``self._rows``：刷新后旧行已被
        deleteLater，直接捕获行对象会摸到已删除的 C++ 对象（无 traceback 崩退）。
        """
        row = self._rows.get(trace_id)
        if row is None:
            return
        if row.is_open:
            row.set_open(False)
            row.clear_detail()
            if self._expanded_id == trace_id:
                self._expanded_id = None
            return

        # 手风琴：同时只开一条（多条展开会把页面撑得很长、也容易看串）
        prev = self._rows.get(self._expanded_id or "")
        if prev is not None and prev is not row:
            prev.set_open(False)
            prev.clear_detail()
        self._expanded_id = trace_id
        row.set_open(True)
        row.set_message("读取明细中…")

        worker = LLMWorker(api_guard(lambda: api.get_trace_detail(trace_id)))
        worker.success.connect(
            lambda payload, tid=trace_id: self._fill_trace_detail(tid, payload))
        worker.failed.connect(
            lambda msg, tid=trace_id: self._fail_trace_detail(tid, msg))
        worker.start()

    def _fill_trace_detail(self, trace_id: str, payload: Any) -> None:
        row = self._rows.get(trace_id)
        if row is None or not isinstance(payload, dict):
            return
        row.set_detail(payload)

    def _fail_trace_detail(self, trace_id: str, msg: str) -> None:
        row = self._rows.get(trace_id)
        if row is None:
            return
        row.set_message(f"读取明细失败：{msg}")

    def showEvent(self, event):
        """进入本页就刷新（用量随时在变）。

        注意：**不在构造期**发请求 —— 主窗口会一次性构造全部页面，若每页都在
        构造期起后台线程，启动瞬间会并发一堆请求；且页面还没显示时线程未结束
        就销毁会触发 `QThread: Destroyed while thread is still running`。
        """
        super().showEvent(event)
        self._load()
