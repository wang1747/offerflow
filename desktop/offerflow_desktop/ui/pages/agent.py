"""求职 Agent 页面（桌面版，对齐网页版 AgentConsole.tsx / openCareerAgent.ts）。

设计要点（来自 v0.2-beta 已验证契约）：
- 这是**跨模块编排器**，本身不持有业务数据；所有能力都来自云端 Agent 服务
  （LLM BYOK + LangGraph 确认断点），因此**不能离线运行**。
- 硬规则（与网页版逐字一致）：
  * 待确认写操作的「从 X → Y」**只**取自服务端 `pending_action.expected_status`
    / `new_status`，本地绝不重算、绝不猜测。
  * `write_failure.code` 直接读服务端原值；失败提示按 code 查表，查不到就原文展示，
    **不**拿中文文案去匹配。
  * 确认/拒绝失败的「状态分支」由服务端按 HTTP 状态码决定（410 过期、409 已处理/冲突、404 找不到），桌面端只透出服务端错误原文，不按中文文案判断。
    **不**匹配服务端中文文案。
- 异步走 `LLMWorker` + `components.api_guard`：网络/`ApiError` 会被归一成 `LLMError`，
  页面统一在 `failed` 信号里拿到干净提示。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSizePolicy, QSpacerItem, QVBoxLayout, QWidget,
)

from ... import api_client as api
from ... import config
from ... import database
from ...llm import LLMWorker
from ..components import (
    api_guard, card, empty_state, hint_label, make_title, primary_button,
    scroll_content, section_label, set_prop, soft_chip,
)


# ===================== 标签映射（逐字移植自 AgentConsole.tsx） =====================
DECISION_LABELS = {
    "apply_now": "立即投递",
    "optimize_first": "先优化简历",
    "collect_evidence": "先补证据",
    "wait": "暂缓",
    "skip": "跳过",
}
RUN_STATUS_LABELS = {
    "completed": "已完成",
    "failed": "失败",
    "needs_input": "需要补充上下文",
    "timeout": "超时",
    "waiting_confirmation": "等待你确认",
    "cancelled": "已取消",
}
INTENT_STATUS_LABELS = {
    "pending_confirmation": "等待你确认",
    "confirmed": "已确认",
    "succeeded": "已执行",
    "cancelled": "已取消",
    "expired": "已过期",
    "failed": "执行失败",
}
MEMORY_STATUS_LABELS = {
    "proposed": "待确认",
    "confirmed": "已确认",
    "superseded": "已被取代",
    "rejected": "已拒绝",
    "expired": "已过期",
}
MEMORY_TYPE_LABELS = {
    "preference": "偏好",
    "decision": "决策",
    "outcome": "结果",
    "constraint": "约束",
    "fact": "事实",
}
WRITE_FAILURE_HINTS = {
    "status_conflict": "版本冲突：你或系统改过投递状态，本轮没有写入任何数据。请重新问一次 Agent 拿最新的建议。",
    "invalid_status": "目标状态不合法，本轮没有写入任何数据。",
    "status_not_agent_writable": "该状态不允许由 Agent 修改，本轮没有写入任何数据。",
    "not_found": "找不到对应的投递记录，本轮没有写入任何数据。",
    "write_failed": "写入业务数据时出错，本轮没有写入任何数据。",
    "action_expired": "这条确认已过期，本轮没有写入任何数据。请重新问一次 Agent。",
    "action_already_resolved": "这条操作已经处理过了，本轮没有写入任何数据。",
}
CHECKPOINTER_HINTS = {
    "postgres": "会话与确认断点持久化在数据库，服务重启后仍可恢复。",
    "memory": "会话只存在进程内存里，服务重启后无法恢复。",
    "uninitialized": "Agent 运行时尚未初始化，会话不可恢复。",
}

# ===================== 会话列表（本机持久化） =====================
# 对齐网页版 AgentConsole 的 localStorage 那份（key = of_agent_sessions_v1）。
# 桌面版没有 localStorage，落到本机 SQLite 的 drafts 表 —— 那张表就是给
# 「本机行为、不该上行云端」的数据准备的（见 database.save_draft 的注释）。
# **不要**改走 local_store：那会把会话列表塞进 outbox 往服务端同步。
_SESSIONS_SCOPE = "agent_sessions_v1"
_SESSIONS_MAX = 20  # 与网页版一致：最新置顶，最多 20 条


def _load_sessions() -> List[Dict[str, Any]]:
    """从本机库读会话列表；读不到/格式坏 → 空列表（不抛）。"""
    rec = database.load_draft(_SESSIONS_SCOPE)
    data = (rec or {}).get("payload")
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in data:
        if isinstance(it, dict) and str(it.get("session_id") or "").strip():
            out.append(it)
    return out[:_SESSIONS_MAX]


def _persist_sessions(items: List[Dict[str, Any]]) -> None:
    """写回会话列表；写失败静默（和网页版 localStorage 不可用时一样，不打断主流程）。"""
    try:
        database.save_draft(_SESSIONS_SCOPE, list(items)[:_SESSIONS_MAX])
    except Exception:  # noqa: BLE001 - 持久化失败不该影响会话本身
        pass


def _format_session_time(raw: Any) -> str:
    """把存下来的时间戳显示成 ``MM-DD HH:MM``；认不出就原样返回。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        return text

# 颜色（HEX，给 soft_chip 用；不用 8 位写法，避免 Qt 解析成 #AARRGGBB）
_C_GREEN = "#059669"
_C_RED = "#dc2626"
_C_AMBER = "#d97706"
_C_GRAY = "#94a3b8"
_C_INDIGO = "#4f46e5"
_C_BLUE = "#1d4ed8"
_C_CYAN = "#0891b2"


def _run_status_color(status: str) -> str:
    if status == "completed":
        return _C_GREEN
    if status in ("failed",):
        return _C_RED
    if status in ("needs_input", "waiting_confirmation"):
        return _C_AMBER
    return _C_GRAY


def _tool_status_color(status: str) -> str:
    if status == "succeeded":
        return _C_GREEN
    if status == "failed":
        return _C_RED
    return _C_GRAY


def _intent_status_color(status: str) -> str:
    if status in ("succeeded", "expired", "failed"):
        return _C_RED
    if status in ("cancelled",):
        return _C_GRAY
    return _C_AMBER  # pending_confirmation / confirmed


def _status_label(status: str) -> str:
    """投递状态 code → 中文（用桌面端 config，与网页版一致）。"""
    return config.STATUS_LABELS.get(status, status)


def _clear(box: QVBoxLayout) -> None:
    """清空布局里的所有控件（含 spacer）。"""
    while box.count():
        item = box.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


class AgentPage(QWidget):
    """求职 Agent 控制台（单栏布局，镜像网页版 AgentConsole）。"""

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window

        # ---- 状态 ----
        self._session: Optional[Dict[str, Any]] = None
        self._run: Optional[Dict[str, Any]] = None
        self._trace: Optional[Dict[str, Any]] = None
        self._pending_actions: List[Dict[str, Any]] = []
        self._memories: List[Dict[str, Any]] = []
        self._jd_list: List[Dict[str, Any]] = []
        self._resume_list: List[Dict[str, Any]] = []
        self._app_map: Dict[str, Dict[str, Any]] = {}
        self._picked_jd = ""
        self._picked_resume = ""
        self._entry: Dict[str, Any] = {}
        self._error: Optional[str] = None
        self._memory_error: Optional[str] = None
        self._busy = False
        self._memory_busy = False
        self._message = "我下一步该做什么？"
        # 本机会话列表（最新置顶；见 _remember_session）。
        # **构造期不读盘**：这个项目的硬规矩是「构造只搭界面」——在 __init__ 里
        # 做 IO/发请求，会在 logout→relogin 重建 MainWindow 时卡住或崩进程。
        # 真正的读取放在 showEvent（_load_local_sessions）。
        self._local_sessions: List[Dict[str, Any]] = []

        # ---- 生命周期守卫 ----
        # 上下文候选改为「首次 showEvent 才拉」，构造期绝不起后台线程
        # （否则 main.py 登录循环重建 MainWindow 时，旧 AgentPage 已销毁但
        #  LLMWorker 仍在跑，回调访问已释放 Qt 对象 → 0xC0000409 静默崩）。
        self._contexts_loaded = False
        self._workers: List[LLMWorker] = []

        # ---- 根布局：标题 + 错误条全宽；下面三栏各自独立滚动 ----
        # 三栏**各自独立滚动**，而不是整页一条长滚动：否则右栏（运行轨迹 +
        # 长期记忆）一多就把整页拉得很长、中栏留大片空白，观感与网页版三栏
        # 不一致。对齐网页版 AgentConsole：左=会话/上下文选择，中=对话流，
        # 右=当前上下文 + 运行轨迹 + 长期记忆。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QVBoxLayout()
        head.setContentsMargins(24, 20, 24, 20)
        head.setSpacing(12)
        root.addLayout(head)
        self._head_box = head

        cols = QHBoxLayout()
        cols.setContentsMargins(24, 20, 24, 20)
        cols.setSpacing(12)
        root.addLayout(cols, 1)

        self._left_box = QVBoxLayout()
        self._mid_box = QVBoxLayout()
        self._right_box = QVBoxLayout()
        for box in (self._left_box, self._mid_box, self._right_box):
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(12)

        left_scroll = scroll_content(self._left_box)
        left_scroll.setFixedWidth(280)
        left_scroll.setObjectName("agent-col-left")
        right_scroll = scroll_content(self._right_box)
        right_scroll.setFixedWidth(360)
        right_scroll.setObjectName("agent-col-right")
        mid_scroll = scroll_content(self._mid_box)
        mid_scroll.setMinimumWidth(360)
        mid_scroll.setObjectName("agent-col-mid")
        cols.addWidget(left_scroll)
        cols.addWidget(mid_scroll, 1)
        cols.addWidget(right_scroll)

        # 对话流卡片（输入/结果/待确认/写入结果/证据）沿用 _content_box，
        # 因此它们全部落在中栏，无需改动。
        self._content_box = self._mid_box

        # 离屏加载遮罩（盖住整页）
        self._overlay = None  # 在 _build_ui 末尾创建，parent=self

        self._build_ui()
        self._overlay = _BusyOverlay(self)
        self._render()

        # 页面销毁时断开仍在跑的后台任务回调，防止回调访问已释放 Qt 对象。
        # 注：AgentPage 是 QStackedWidget 的子页面，parent 关闭时不会触发它的
        # closeEvent，destroyed 才是子 widget 确定性析构钩子。
        self.destroyed.connect(self._cleanup_workers)

    # ===================== 生命周期 =====================
    def showEvent(self, event) -> None:
        """首次显示时再拉上下文候选（构造期不再起后台线程）。"""
        super().showEvent(event)
        if self._contexts_loaded:
            return
        self._contexts_loaded = True
        self._load_local_sessions()
        self._load_contexts()

    def _load_local_sessions(self) -> None:
        """从本机库读会话历史（幂等：只做一次合并，不覆盖内存里已有的）。

        合并而不是直接覆盖：入口参数（open_with）可能先建了会话并已落盘，
        读回来的列表必须与内存里那份并起来，不能把刚建的会话顶掉。
        """
        stored = _load_sessions()
        seen = {str(x.get("session_id") or "") for x in self._local_sessions}
        for item in stored:
            sid = str(item.get("session_id") or "")
            if sid and sid not in seen:
                self._local_sessions.append(item)
                seen.add(sid)
        self._local_sessions = self._local_sessions[:_SESSIONS_MAX]
        self._render_sessions()

    def _cleanup_workers(self) -> None:
        """页面销毁前断开所有后台任务回调。

        不直接 wait()/terminate()：worker 在执行网络请求，GUI 线程 wait 会卡 UI，
        且 terminate 不安全；isValid 守卫已能兜住回调访问已释放对象的情况，
        这里断开信号即可让回调不再触发（worker 跑完自会从 _ALIVE 移除）。
        """
        for w in self._workers:
            try:
                w.success.disconnect()
                w.failed.disconnect()
            except Exception:
                pass
        self._workers = []

    # ===================== UI 骨架 =====================
    def _build_ui(self) -> None:
        self._head_box.addWidget(make_title(
            "求职 Agent",
            "读 JD 与简历给出行动建议；改业务数据的操作必须经你确认。",
        ))

        # 页面级错误条
        self._error_label = QLabel()
        self._error_label.setProperty("class", "alert-error")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        self._head_box.addWidget(self._error_label)

        # ---- 上下文卡片 ----
        ctx_card = card()
        ctx_lay = QVBoxLayout(ctx_card)
        ctx_lay.setContentsMargins(16, 14, 16, 14)
        ctx_lay.setSpacing(8)
        ctx_top = QHBoxLayout()
        ctx_top.setContentsMargins(0, 0, 0, 0)
        ctx_top.setSpacing(8)
        ctx_top.addWidget(section_label("当前上下文"))
        ctx_top.addStretch(1)
        self._ctx_checkpointer = soft_chip("尚未建会话", _C_GRAY)
        self._ctx_memory = soft_chip("长期记忆未启用", _C_GRAY)
        ctx_top.addWidget(self._ctx_checkpointer)
        ctx_top.addWidget(self._ctx_memory)
        ctx_lay.addLayout(ctx_top)
        self._ctx_box = QVBoxLayout()
        self._ctx_box.setContentsMargins(0, 0, 0, 0)
        self._ctx_box.setSpacing(8)
        ctx_lay.addLayout(self._ctx_box)
        self._right_box.addWidget(ctx_card)

        # ---- 会话列表（本机持久化；对齐网页版左栏的「会话」卡片）----
        sess_card = card()
        sess_lay = QVBoxLayout(sess_card)
        sess_lay.setContentsMargins(16, 14, 16, 14)
        sess_lay.setSpacing(8)
        sess_top = QHBoxLayout()
        sess_top.setContentsMargins(0, 0, 0, 0)
        sess_top.setSpacing(8)
        sess_top.addWidget(section_label("会话"))
        sess_top.addStretch(1)
        self._new_btn = QPushButton("新建")
        self._new_btn.setProperty("class", "ghost")
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self._on_reset)
        sess_top.addWidget(self._new_btn)
        sess_lay.addLayout(sess_top)
        self._sessions_box = QVBoxLayout()
        self._sessions_box.setContentsMargins(0, 0, 0, 0)
        self._sessions_box.setSpacing(6)
        sess_lay.addLayout(self._sessions_box)
        self._left_box.addWidget(sess_card)

        # ---- 上下文选择器 ----
        sel_card = card()
        sel_lay = QVBoxLayout(sel_card)
        sel_lay.setContentsMargins(16, 14, 16, 14)
        sel_lay.setSpacing(8)
        sel_lay.addWidget(section_label("选择上下文"))
        self._missing_box = QVBoxLayout()
        self._missing_box.setContentsMargins(0, 0, 0, 0)
        self._missing_box.setSpacing(4)
        sel_lay.addLayout(self._missing_box)

        # JD 与简历**上下排列**：左栏只有 ~250px，两个 combo 并排（各 220）会把卡片
        # 最小宽撑到 480px，直接顶出横向滚动条。
        pick_row = QVBoxLayout()
        pick_row.setSpacing(8)
        jd_col = QVBoxLayout()
        jd_col.setSpacing(4)
        jd_col.addWidget(QLabel("目标 JD"))
        self._jd_combo = QComboBox()
        self._jd_combo.currentIndexChanged.connect(self._on_pick_changed)
        jd_col.addWidget(self._jd_combo)
        resume_col = QVBoxLayout()
        resume_col.setSpacing(4)
        resume_col.addWidget(QLabel("使用简历"))
        self._resume_combo = QComboBox()
        self._resume_combo.currentIndexChanged.connect(self._on_pick_changed)
        resume_col.addWidget(self._resume_combo)
        pick_row.addLayout(jd_col)
        pick_row.addLayout(resume_col)
        sel_lay.addLayout(pick_row)

        self._switch_hint = hint_label(
            "所选上下文与当前会话不同。切换上下文会新建一个会话，当前会话保持不变，"
            "不会静默替换上下文。", shrink=True)
        self._switch_hint.hide()
        sel_lay.addWidget(self._switch_hint)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._create_btn = primary_button("开始会话")
        self._create_btn.clicked.connect(self._on_create_or_reset)
        self._reset_btn = QPushButton("清空本页会话")
        self._reset_btn.setProperty("class", "ghost")
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.clicked.connect(self._on_reset)
        self._reset_btn.hide()
        btn_row.addWidget(self._create_btn)
        btn_row.addWidget(self._reset_btn)
        btn_row.addStretch(1)
        sel_lay.addLayout(btn_row)
        self._left_box.addWidget(sel_card)

        # ---- 输入 ----
        in_card = card()
        in_lay = QVBoxLayout(in_card)
        in_lay.setContentsMargins(16, 14, 16, 14)
        in_lay.setSpacing(8)
        in_lay.addWidget(section_label("问 Agent"))
        self._msg_edit = QPlainTextEdit(self._message)
        self._msg_edit.setFixedHeight(72)
        self._msg_edit.textChanged.connect(self._on_msg_changed)
        in_lay.addWidget(self._msg_edit)
        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self._run_btn = primary_button("运行")
        self._run_btn.clicked.connect(self._on_run)
        run_row.addWidget(self._run_btn)
        run_row.addStretch(1)
        in_lay.addLayout(run_row)
        # 这条提示原先跟在按钮右边：整句中文的 minimumSizeHint 有 396px，会把中栏
        # 顶出横向滚动条 —— 移到按钮下方并允许换行收缩。
        in_lay.addWidget(hint_label(
            "同一会话同一时刻只能有一条消息在执行（按钮已禁用，避免重复提交）。",
            shrink=True))
        self._content_box.addWidget(in_card)

        # ---- 运行结果 ----
        self._result_card = card(highlight=False)
        self._result_box = QVBoxLayout(self._result_card)
        self._result_box.setContentsMargins(16, 14, 16, 14)
        self._result_box.setSpacing(8)
        self._result_card.hide()
        self._content_box.addWidget(self._result_card)

        # ---- 待确认的写操作 ----
        self._pending_card = card(warn=True)
        self._pending_box = QVBoxLayout(self._pending_card)
        self._pending_box.setContentsMargins(16, 14, 16, 14)
        self._pending_box.setSpacing(8)
        self._pending_card.hide()
        self._content_box.addWidget(self._pending_card)

        # ---- 写操作结果：成功 ----
        self._wrote_card = card(success=True)
        wrote_lay = QVBoxLayout(self._wrote_card)
        wrote_lay.setContentsMargins(16, 14, 16, 14)
        wrote_lbl = QLabel("已按你的确认写入业务数据（这一轮 Agent 真的改了投递状态）。")
        wrote_lbl.setProperty("class", "ok")
        wrote_lbl.setWordWrap(True)
        wrote_lay.addWidget(wrote_lbl)
        self._wrote_card.hide()
        self._content_box.addWidget(self._wrote_card)

        # ---- 写操作结果：失败 ----
        self._failure_card = card(danger=True)
        self._failure_box = QVBoxLayout(self._failure_card)
        self._failure_box.setContentsMargins(16, 14, 16, 14)
        self._failure_box.setSpacing(8)
        self._failure_card.hide()
        self._content_box.addWidget(self._failure_card)

        # ---- 证据 ----
        self._evidence_card = card()
        self._evidence_box = QVBoxLayout(self._evidence_card)
        self._evidence_box.setContentsMargins(16, 14, 16, 14)
        self._evidence_box.setSpacing(8)
        self._evidence_card.hide()
        self._content_box.addWidget(self._evidence_card)

        # ---- 运行轨迹 ----
        trace_card = card()
        trace_lay = QVBoxLayout(trace_card)
        trace_lay.setContentsMargins(16, 14, 16, 14)
        trace_lay.setSpacing(8)
        trace_top = QHBoxLayout()
        trace_top.setContentsMargins(0, 0, 0, 0)
        trace_top.setSpacing(8)
        trace_top.addWidget(section_label("运行轨迹"))
        trace_top.addStretch(1)
        self._trace_refresh_btn = QPushButton("刷新运行轨迹")
        self._trace_refresh_btn.setProperty("class", "ghost")
        self._trace_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._trace_refresh_btn.clicked.connect(self._on_load_trace)
        trace_top.addWidget(self._trace_refresh_btn)
        trace_lay.addLayout(trace_top)
        self._trace_box = QVBoxLayout()
        self._trace_box.setContentsMargins(0, 0, 0, 0)
        self._trace_box.setSpacing(8)
        trace_lay.addLayout(self._trace_box)
        self._right_box.addWidget(trace_card)

        # ---- 长期记忆 ----
        mem_card = card()
        mem_lay = QVBoxLayout(mem_card)
        mem_lay.setContentsMargins(16, 14, 16, 14)
        mem_lay.setSpacing(8)
        mem_top = QHBoxLayout()
        mem_top.setContentsMargins(0, 0, 0, 0)
        mem_top.setSpacing(8)
        mem_top.addWidget(section_label("长期记忆"))
        mem_top.addStretch(1)
        self._mem_refresh_btn = QPushButton("刷新记忆")
        self._mem_refresh_btn.setProperty("class", "ghost")
        self._mem_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mem_refresh_btn.clicked.connect(self._refresh_memories)
        mem_top.addWidget(self._mem_refresh_btn)
        mem_lay.addLayout(mem_top)
        mem_lay.addWidget(hint_label(
            "「待确认」是 Agent 从你的简历与 JD 推断出的，**不会被当成事实召回**；"
            "你确认之后它才进入记忆。你可以随时删除任何一条。", shrink=True))
        self._mem_error_label = QLabel()
        self._mem_error_label.setProperty("class", "alert-error")
        self._mem_error_label.setWordWrap(True)
        self._mem_error_label.hide()
        mem_lay.addWidget(self._mem_error_label)
        self._proposed_box = QVBoxLayout()
        self._proposed_box.setContentsMargins(0, 0, 0, 0)
        self._proposed_box.setSpacing(8)
        self._confirmed_box = QVBoxLayout()
        self._confirmed_box.setContentsMargins(0, 0, 0, 0)
        self._confirmed_box.setSpacing(8)
        mem_lay.addLayout(self._proposed_box)
        mem_lay.addLayout(self._confirmed_box)

        add_sec = QVBoxLayout()
        add_sec.setContentsMargins(0, 8, 0, 0)
        add_sec.setSpacing(4)
        add_sec.addWidget(QLabel("直接加一条（你说的话直接生效，不需要再确认）"))
        self._mem_edit = QPlainTextEdit()
        self._mem_edit.setFixedHeight(52)
        self._mem_edit.setPlaceholderText("例如：我只考虑北京的机会")
        add_sec.addWidget(self._mem_edit)
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self._mem_add_btn = primary_button("添加")
        self._mem_add_btn.clicked.connect(self._on_add_memory)
        add_row.addWidget(self._mem_add_btn)
        add_row.addStretch(1)
        add_sec.addLayout(add_row)
        # 同中栏：整句中文的 minimumSizeHint 会撑宽右栏 → 单独一行 + 可收缩换行
        add_sec.addWidget(hint_label("内容限 500 字、最多 3 行；长文与凭据类内容会被服务端拒绝。",
                                     shrink=True))
        mem_lay.addLayout(add_sec)
        self._right_box.addWidget(mem_card)

        # 三栏末尾各放一个 Expanding spacer，吃掉栏内剩余高度。
        # 不能省：卡片竖直是 Preferred，没有 spacer 时多余高度会被卡片内部的标签吃掉
        # （表现为「标题飘在卡片中间」，section-title 被撑到 100~280px）。
        # 也不要用 setAlignment(AlignTop) 代替 —— 它挡不住 Preferred 卡片扩张，实测更糟。
        # 必须是 addStretch(1)（stretch=1）而不是 addSpacerItem(...,stretch=0)：
        # stretch=0 的 spacer 与卡片同等对待，剩余高度仍会被 Preferred 卡片分走一部分
        # （实测 in_card 从 191 被拉到 358，标题与提示各被撑到 ~104px）。
        for _box in (self._left_box, self._mid_box, self._right_box):
            _box.addStretch(1)

    # ===================== 异步调度 =====================
    def _run_job(self, fn, on_ok, on_err=None, memory=False) -> None:
        if memory:
            self._set_memory_busy(True)
        else:
            self._set_busy(True)

        def ok(result):
            # 页面可能已被销毁（logout→relogin 重建 MainWindow），此时 self 的
            # C++ 对象已释放，访问任何 Qt 属性都会 0xC0000409。isValid 守卫
            # 是必须的——LLMWorker 由模块级 _ALIVE 强引用保活，回调一定会跑。
            if not isValid(self):
                return
            if memory:
                self._set_memory_busy(False)
            else:
                self._set_busy(False)
            on_ok(result)

        def fail(msg):
            if not isValid(self):
                return
            if memory:
                self._set_memory_busy(False)
            else:
                self._set_busy(False)
            if on_err:
                on_err(msg)
            else:
                self._show_error(msg)

        w = LLMWorker(api_guard(fn))
        w.success.connect(ok)
        w.failed.connect(fail)
        w.start()
        self._workers.append(w)

    def _set_busy(self, b: bool) -> None:
        self._busy = b
        if b:
            self._overlay.show_over("Agent 运行中…")
        else:
            self._overlay.hide()
        self._run_btn.setEnabled(
            not b and self._session is not None and bool(self._message.strip()))
        self._create_btn.setEnabled(not b)
        self._reset_btn.setEnabled(not b)
        self._new_btn.setEnabled(not b)

    def _set_memory_busy(self, b: bool) -> None:
        self._memory_busy = b
        self._mem_add_btn.setEnabled(not b and bool(self._mem_edit.toPlainText().strip()))
        self._mem_refresh_btn.setEnabled(not b)

    def _show_error(self, msg: str) -> None:
        self._error = msg
        self._error_label.setText(msg)
        self._error_label.show()

    def _clear_error(self) -> None:
        self._error = None
        self._error_label.hide()

    # ===================== 上下文候选 =====================
    def _load_contexts(self) -> None:
        """拉 JD / 简历 / 投递列表（用于选择器与名称解析）。失败不阻塞页面。"""
        def job():
            jds = api.list_jds()
            resumes = api.list_resumes()
            apps = api.list_applications()
            return jds, resumes, apps
        self._run_job(job, self._on_contexts_loaded, on_err=self._on_contexts_error)

    def _on_contexts_loaded(self, payload) -> None:
        jds, resumes, apps = payload
        self._jd_list = jds or []
        self._resume_list = resumes or []
        self._app_map = {a.get("id"): a for a in (apps or []) if isinstance(a, dict)}
        self._fill_combos()
        # 若带着入口参数进来（open_with），按它建/恢复会话
        if self._entry:
            self._apply_entry(self._entry)
            self._entry = {}

    def _on_contexts_error(self, msg: str) -> None:
        # 上下文拉不到不影响已有会话渲染；仅记录，不弹错误条
        self._memory_error = None
        # 仍然尝试按入口恢复（即使列表为空，建会话只需要 jd_id/resume_id 字符串）
        if self._entry:
            self._apply_entry(self._entry)
            self._entry = {}

    def _fill_combos(self) -> None:
        jd_names = {j.get("id"): self._jd_name(j) for j in self._jd_list}
        resume_names = {r.get("id"): self._resume_name(r) for r in self._resume_list}
        self._jd_combo.clear()
        self._jd_combo.addItem("请选择 JD", "")
        for j in self._jd_list:
            self._jd_combo.addItem(jd_names.get(j.get("id"), j.get("id", "")), j.get("id", ""))
        self._resume_combo.clear()
        self._resume_combo.addItem("请选择简历", "")
        for r in self._resume_list:
            self._resume_combo.addItem(resume_names.get(r.get("id"), r.get("id", "")), r.get("id", ""))

    def _jd_name(self, item: Dict[str, Any]) -> str:
        parts = [item.get("company"), item.get("position")]
        return " · ".join([p for p in parts if p]) or item.get("id", "")

    def _resume_name(self, item: Dict[str, Any]) -> str:
        base = item.get("source_file") or item.get("source") or "简历"
        ver = item.get("version")
        return f"{base} · v{ver}" if ver else base

    def _app_name(self, app_id: Optional[str]) -> str:
        if not app_id:
            return "—"
        a = self._app_map.get(app_id)
        if not a:
            return app_id
        return " · ".join([p for p in (a.get("company"), a.get("position")) if p]) or app_id

    @staticmethod
    def _lookup(item_id: Optional[str], items: List[Dict[str, Any]]) -> Dict[str, Any]:
        for it in items:
            if it.get("id") == item_id:
                return it
        return {"id": item_id}

    # ===================== 入口（对齐 openCareerAgent.ts） =====================
    def open_with(self, entry: Dict[str, Any]) -> None:
        """业务页面带着上下文进入 Agent（唯一入口，业务页不直接调 api.agent.*）。

        entry 允许字段：entry_source, jd_id, resume_id, application_id, session_id。
        桌面端不传 session_id 来回跳——总是按上下文创建/恢复会话；
        但若带了 session_id 则按它恢复（刷新/历史返回语义）。
        """
        self._entry = entry or {}
        # 入口参数先落到选择器（即使上下文列表还没回来也能预填）
        self._picked_jd = (entry or {}).get("jd_id") or ""
        self._picked_resume = (entry or {}).get("resume_id") or ""
        self._set_combo(self._jd_combo, self._picked_jd)
        self._set_combo(self._resume_combo, self._picked_resume)
        # 若上下文已经加载过，立即应用；否则等 _on_contexts_loaded
        if self._jd_list or self._resume_list or (entry or {}).get("session_id"):
            self._apply_entry(entry or {})

    def _apply_entry(self, entry: Dict[str, Any]) -> None:
        if entry.get("session_id"):
            self._run_job(lambda: api.agent_get_session(entry["session_id"]),
                          self._on_session_loaded)
            return
        if entry.get("jd_id") or entry.get("resume_id"):
            self._create_session(
                entry.get("jd_id") or "", entry.get("resume_id") or "",
                entry.get("entry_source") or "from_page")

    @staticmethod
    def _set_combo(combo: QComboBox, data: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    # ===================== 会话创建 / 重置 =====================
    def _on_create_or_reset(self) -> None:
        if self._session is None:
            self._create_session(self._picked_jd, self._picked_resume)
        else:
            # 以所选上下文新建会话（不替换当前会话）
            self._create_session(self._picked_jd, self._picked_resume)

    def _create_session(self, jd_id: str, resume_id: str, entry_source: str = "from_page") -> None:
        self._clear_error()
        self._run_job(
            lambda: api.agent_create_session(
                entry_source=entry_source, jd_id=jd_id or None,
                resume_id=resume_id or None,
                application_id=self._entry.get("application_id") if self._entry else None),
            self._on_session_created)

    def _on_session_created(self, session: Dict[str, Any]) -> None:
        if not session:
            self._show_error("服务端未返回会话")
            return
        self._session = session
        self._run = None
        self._trace = None
        self._apply_session_to_controls(session)
        self._remember_session(session)
        # 创建后重拉待确认与记忆（刚进来的会话一般没有，但保持一致）
        self._refresh_pending()
        self._refresh_memories()
        self._render()

    def _on_session_loaded(self, session: Dict[str, Any], remember: bool = True) -> None:
        if not session:
            self._show_error("找不到该会话（可能已被清理或不属于你）")
            return
        self._session = session
        self._apply_session_to_controls(session)
        if remember:
            self._remember_session(session)
        self._refresh_pending()
        self._render()

    def _apply_session_to_controls(self, session: Dict[str, Any]) -> None:
        refs = session.get("context_refs") or []
        jd_id = next((r.get("id") for r in refs if r.get("type") == "jd"), "")
        resume_id = next((r.get("id") for r in refs if r.get("type") == "resume"), "")
        self._picked_jd = jd_id
        self._picked_resume = resume_id
        self._set_combo(self._jd_combo, jd_id)
        self._set_combo(self._resume_combo, resume_id)

    def _on_reset(self) -> None:
        self._session = None
        self._run = None
        self._trace = None
        self._pending_actions = []
        self._render()

    # ===================== 运行消息 =====================
    def _on_msg_changed(self) -> None:
        self._message = self._msg_edit.toPlainText()
        self._run_btn.setEnabled(
            not self._busy and self._session is not None and bool(self._message.strip()))

    def _on_run(self) -> None:
        if not self._session:
            return
        text = self._msg_edit.toPlainText().strip()
        if not text:
            return
        self._clear_error()
        sid = self._session["session_id"]
        self._run_job(lambda: api.agent_run_message(sid, text), self._on_run_done)

    def _on_run_done(self, run: Dict[str, Any]) -> None:
        self._run = run or {}
        # 运行完重新拉会话（status / run_count / last_output 以服务端为准）
        sid = self._session["session_id"] if self._session else None
        if sid:
            self._run_job(lambda: api.agent_get_session(sid), self._on_session_reloaded,
                          on_err=lambda _m: None)
        self._refresh_pending()
        self._refresh_memories()
        self._render()

    def _on_session_reloaded(self, session: Dict[str, Any]) -> None:
        if session:
            self._session = session
            self._apply_session_to_controls(session)
            self._render()

    # ===================== 待确认写操作 =====================
    def _refresh_pending(self) -> None:
        if not self._session:
            self._pending_actions = []
            return
        sid = self._session["session_id"]
        self._run_job(lambda: api.agent_list_actions(sid), self._on_pending_loaded,
                      on_err=self._on_pending_error)

    def _on_pending_loaded(self, actions: List[Dict[str, Any]]) -> None:
        self._pending_actions = actions or []
        self._render()

    def _on_pending_error(self, msg: str) -> None:
        # 拉不到待确认列表只影响这一块，不污染整页错误
        self._memory_error = msg
        self._render_memory()

    def _on_decide(self, action_id: str, approved: bool) -> None:
        if not self._session:
            return
        sid = self._session["session_id"]
        self._clear_error()
        self._run_job(
            lambda: api.agent_decide_action(sid, action_id, approved),
            self._on_decide_done,
            on_err=self._on_decide_error)

    def _on_decide_done(self, run: Dict[str, Any]) -> None:
        self._run = run or {}
        sid = self._session["session_id"] if self._session else None
        if sid:
            self._run_job(lambda: api.agent_get_session(sid), self._on_session_reloaded,
                          on_err=lambda _m: None)
        self._refresh_pending()
        self._refresh_memories()
        self._render()

    def _on_decide_error(self, msg: str) -> None:
        # 410 = 已过期：按状态码分支（api_guard 已把 ApiError 转成 LLMError，
        # 这里拿不到 HTTP 码，但服务端过期时失败信息本身已足够明确，
        # 仍提示用户重新问一次 Agent）。其余按通用错误处理。
        self._show_error(msg)
        if self._session:
            self._refresh_pending()

    # ===================== 运行轨迹 =====================
    def _on_load_trace(self) -> None:
        if not self._session:
            return
        sid = self._session["session_id"]
        self._run_job(lambda: api.agent_get_trace(sid), self._on_trace_loaded)

    def _on_trace_loaded(self, trace: Dict[str, Any]) -> None:
        self._trace = trace or {}
        self._render_trace()

    # ===================== 长期记忆 =====================
    def _refresh_memories(self) -> None:
        self._run_job(api.agent_list_memories, self._on_memories_loaded,
                      on_err=self._on_memories_error, memory=True)

    def _on_memories_loaded(self, memories: List[Dict[str, Any]]) -> None:
        self._memories = memories or []
        self._memory_error = None
        self._mem_error_label.hide()
        self._render_memory()

    def _on_memories_error(self, msg: str) -> None:
        self._memory_error = msg
        self._mem_error_label.setText(msg)
        self._mem_error_label.show()

    def _on_add_memory(self) -> None:
        content = self._mem_edit.toPlainText().strip()
        if not content:
            return
        self._mem_edit.setPlainText("")
        self._run_job(lambda: api.agent_create_memory(content),
                      lambda _r: None, on_err=self._on_memories_error, memory=True)

    def _on_confirm_memory(self, memory_id: str) -> None:
        self._run_job(lambda: api.agent_confirm_memory(memory_id),
                      lambda _r: None, on_err=self._on_memories_error, memory=True)

    def _on_delete_memory(self, memory_id: str) -> None:
        self._run_job(lambda: api.agent_delete_memory(memory_id),
                      lambda _r: None, on_err=self._on_memories_error, memory=True)

    # ===================== 渲染 =====================
    def _render(self) -> None:
        self._render_sessions()
        self._render_context()
        self._render_selector()
        self._render_result()
        self._render_pending()
        self._render_wrote()
        self._render_failure()
        self._render_evidence()
        self._render_trace()
        self._render_memory()

    # ===================== 会话列表（左栏） =====================
    def _render_sessions(self) -> None:
        """重建左栏的会话列表。空 → 一句占位提示（对齐网页版文案）。"""
        _clear(self._sessions_box)
        if not self._local_sessions:
            self._sessions_box.addWidget(
                hint_label("还没有保存的会话。", shrink=True))
            return
        cur = (self._session or {}).get("session_id") or ""
        for item in self._local_sessions:
            sid = str(item.get("session_id") or "")
            if not sid:
                continue
            btn = QPushButton()
            ctx = str(item.get("context") or "未选择上下文")
            when = _format_session_time(item.get("updated_at"))
            btn.setText(f"{ctx}\n{when}" if when else ctx)
            btn.setProperty("class", "session-item")
            set_prop(btn, "active", "true" if sid == cur else "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(sid)
            btn.clicked.connect(lambda _c=False, _sid=sid: self._on_restore_session(_sid))
            self._sessions_box.addWidget(btn)

    def _session_context_label(self, session: Dict[str, Any]) -> str:
        """会话的上下文摘要（对齐网页版 rememberSession 里那段拼接）。"""
        refs = session.get("context_refs") or []
        jd_id = next((r.get("id") for r in refs if r.get("type") == "jd"), "")
        resume_id = next((r.get("id") for r in refs if r.get("type") == "resume"), "")
        parts: List[str] = []
        if jd_id:
            parts.append(self._jd_name(self._lookup(jd_id, self._jd_list)))
        if resume_id:
            parts.append(self._resume_name(self._lookup(resume_id, self._resume_list)))
        return " · ".join([p for p in parts if p]) or "未选择上下文"

    def _remember_session(self, session: Optional[Dict[str, Any]]) -> None:
        """把会话记进本机列表（最新置顶，最多 20 条）。写盘失败不影响会话本身。"""
        if not session:
            return
        sid = str(session.get("session_id") or "").strip()
        if not sid:
            return
        rest = [x for x in self._local_sessions
                if str(x.get("session_id") or "") != sid]
        self._local_sessions = [{
            "session_id": sid,
            "context": self._session_context_label(session),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }] + rest
        self._local_sessions = self._local_sessions[:_SESSIONS_MAX]
        _persist_sessions(self._local_sessions)
        self._render_sessions()

    def _on_restore_session(self, session_id: str) -> None:
        """点开左栏历史会话：按它恢复（对齐网页版 restoreSession）。

        桌面端没有地址栏，恢复后**不**把已有会话重置成 null —— 直接替换当前会话。
        与网页版一样：这条只是「切过去看」，不刷新它的置顶时间。
        """
        if not session_id:
            return
        self._clear_error()
        self._run_job(lambda: api.agent_get_session(session_id),
                      self._on_session_restored)

    def _on_session_restored(self, session: Dict[str, Any]) -> None:
        self._on_session_loaded(session, remember=False)

    def _render_context(self) -> None:
        box = self._ctx_box
        _clear(box)
        s = self._session
        # 顶部两个徽章
        if not s:
            self._ctx_checkpointer.setText("尚未建会话")
            self._ctx_checkpointer.setStyleSheet(_chip_style(_C_GRAY))
            self._ctx_memory.setText("长期记忆未启用")
            self._ctx_memory.setStyleSheet(_chip_style(_C_GRAY))
            box.addWidget(hint_label("还没有会话。选择 JD 与简历后开始，或从业务页面带着上下文进入。",
                                     shrink=True))
            return

        cp = s.get("checkpointer", "uninitialized")
        if cp == "postgres":
            self._ctx_checkpointer.setText("会话可恢复")
            self._ctx_checkpointer.setStyleSheet(_chip_style(_C_GREEN))
        elif cp == "memory":
            self._ctx_checkpointer.setText("会话不持久")
            self._ctx_checkpointer.setStyleSheet(_chip_style(_C_GRAY))
        else:
            self._ctx_checkpointer.setText(f"运行时 {cp}")
            self._ctx_checkpointer.setStyleSheet(_chip_style(_C_GRAY))
        if s.get("memory_enabled"):
            self._ctx_memory.setText("长期记忆已启用")
            self._ctx_memory.setStyleSheet(_chip_style(_C_INDIGO))
        else:
            self._ctx_memory.setText("长期记忆未启用")
            self._ctx_memory.setStyleSheet(_chip_style(_C_GRAY))

        grid = QGridLayout()
        grid.setVerticalSpacing(4)
        grid.setHorizontalSpacing(12)
        refs = s.get("context_refs") or []

        def field(row, label, value):
            k = QLabel(label)
            k.setProperty("class", "hint")
            k.setProperty("class", "muted")
            v = QLabel(str(value) if value is not None else "—")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(k, row, 0)
            grid.addWidget(v, row, 1)

        field(0, "会话 session_id", s.get("session_id", ""))
        field(1, "线程 thread_id（应等于 session_id）", s.get("thread_id", ""))
        jd_id = next((r.get("id") for r in refs if r.get("type") == "jd"), "")
        field(2, "目标 JD", self._jd_name(self._lookup(jd_id, self._jd_list)) if jd_id else "未选择")
        resume_id = next((r.get("id") for r in refs if r.get("type") == "resume"), "")
        field(3, "使用简历", self._resume_name(self._lookup(resume_id, self._resume_list)) if resume_id else "未选择")
        field(4, "上下文指纹 context_hash", s.get("context_hash") or "—")
        field(5, "已完成运行数", s.get("run_count", 0))
        box.addLayout(grid)

        # 提示区
        hints = QVBoxLayout()
        hints.setContentsMargins(0, 6, 0, 0)
        hints.setSpacing(4)
        hints.addWidget(hint_label("写操作需要你确认后才会执行；拒绝或过期都不会改动业务数据。",
                                   shrink=True))
        hints.addWidget(hint_label(CHECKPOINTER_HINTS.get(cp, f"checkpointer={cp}"), shrink=True))
        if s.get("has_pending_action"):
            warn = QLabel("这个会话有一项待你确认的写操作，见下方「待确认的写操作」。")
            warn.setProperty("class", "text-warn")
            hints.addWidget(warn)
        for w in (s.get("warnings") or []):
            wl = QLabel(w)
            wl.setProperty("class", "muted")
            wl.setWordWrap(True)
            hints.addWidget(wl)
        box.addLayout(hints)

    def _render_selector(self) -> None:
        s = self._session
        # 缺失上下文告警
        _clear(self._missing_box)
        missing = (s or {}).get("missing_context") or []
        if missing:
            alert = QLabel("上下文不完整，Agent 不会猜测：")
            alert.setProperty("class", "alert-error")
            alert.setWordWrap(True)
            self._missing_box.addWidget(alert)
            for m in missing:
                t = m.get("type", "") if isinstance(m, dict) else str(m)
                hint = m.get("hint", "") if isinstance(m, dict) else ""
                item = QLabel(f"{t}{('（' + hint + '）') if hint else ''}")
                item.setStyleSheet("color: #b91c1c; font-size: 12px; padding-left: 8px;")
                self._missing_box.addWidget(item)

        # 切换提示
        if s:
            refs = s.get("context_refs") or []
            cur_jd = next((r.get("id") for r in refs if r.get("type") == "jd"), "")
            cur_resume = next((r.get("id") for r in refs if r.get("type") == "resume"), "")
            changed = (self._picked_jd or "") != cur_jd or (self._picked_resume or "") != cur_resume
            self._switch_hint.setVisible(changed)
        else:
            self._switch_hint.hide()

        # 创建按钮文案
        self._create_btn.setText("开始会话" if s is None else "以所选上下文新建会话")
        self._reset_btn.setVisible(s is not None)
        self._run_btn.setEnabled(
            not self._busy and s is not None and bool(self._msg_edit.toPlainText().strip()))

    def _render_result(self) -> None:
        run = self._run
        if not run:
            self._result_card.hide()
            return
        self._result_card.show()
        box = self._result_box
        _clear(box)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(section_label("本次运行结果"))
        top.addStretch(1)
        status = run.get("status", "")
        top.addWidget(soft_chip(RUN_STATUS_LABELS.get(status, status), _run_status_color(status)))
        rid = run.get("run_id", "")
        if rid:
            rc = soft_chip(rid, _C_GRAY)
            top.addWidget(rc)
        box.addLayout(top)

        out = run.get("output") or {}
        decision = out.get("decision")
        if decision:
            dec_row = QHBoxLayout()
            dec_row.setContentsMargins(0, 2, 0, 2)
            dec_row.setSpacing(8)
            dec_row.addWidget(QLabel("决策"))
            dec_row.addWidget(soft_chip(DECISION_LABELS.get(decision, decision), _C_INDIGO))
            dec_row.addWidget(QLabel(
                f"置信度 {out.get('confidence', 0)}（服务端原值，未做百分比换算）"))
            dec_row.addWidget(QLabel(f"耗时 {run.get('latency_ms', 0)} ms"))
            dec_row.addStretch(1)
            box.addLayout(dec_row)

        summary = out.get("summary") or "（本次运行没有给出结论文字）"
        box.addWidget(self._selectable(summary))

        missing_info = out.get("missing_info") or []
        if missing_info:
            mi = QLabel("需要你补充：" + "；".join(missing_info))
            mi.setStyleSheet("background:#fffbeb; color:#b45309; border-radius:8px; padding:6px 10px; font-size:12px;")
            mi.setWordWrap(True)
            box.addWidget(mi)

        errors = run.get("errors") or []
        if errors:
            err = QLabel("\n".join(errors))
            err.setProperty("class", "alert-error")
            err.setWordWrap(True)
            box.addWidget(err)

        actions = out.get("actions") or []
        if actions:
            box.addWidget(QLabel("行动建议（本节只是建议文案；需要改业务数据的操作会单独出现在下方「待确认的写操作」里，点了确认才会执行）。"))
            for a in actions:
                box.addWidget(self._action_card(a))

        tool_calls = run.get("tool_calls") or []
        if tool_calls:
            box.addWidget(QLabel("工具调用"))
            for t in tool_calls:
                row = QHBoxLayout()
                row.setContentsMargins(0, 1, 0, 1)
                row.setSpacing(8)
                row.addWidget(soft_chip(t.get("status", ""), _tool_status_color(t.get("status", ""))))
                row.addWidget(QLabel(t.get("tool", "")))
                row.addWidget(QLabel(f"{t.get('latency_ms', 0)} ms"))
                if t.get("error_code"):
                    ec = QLabel(t["error_code"])
                    ec.setProperty("class", "text-danger")
                    row.addWidget(ec)
                row.addStretch(1)
                box.addLayout(row)

    def _action_card(self, a: Dict[str, Any]) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.StyledPanel)
        f.setStyleSheet("border:1px solid #e2e8f0; border-radius:10px; padding:8px 10px;")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(soft_chip(f"P{a.get('priority', 1)}", _C_BLUE))
        title = QLabel(a.get("title", ""))
        title.setStyleSheet("font-weight:600; font-size:13px;")
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)
        if a.get("reason"):
            lay.addWidget(QLabel(a["reason"]))
        refs = a.get("evidence_refs") or []
        if refs:
            lay.addWidget(QLabel("依据：" + " ".join(refs)))
        return f

    def _render_pending(self) -> None:
        active = self._active_intent()
        if not active:
            self._pending_card.hide()
            return
        self._pending_card.show()
        box = self._pending_box
        _clear(box)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(section_label("待确认的写操作"))
        top.addStretch(1)
        st = active.get("status", "")
        top.addWidget(soft_chip(INTENT_STATUS_LABELS.get(st, st), _intent_status_color(st)))
        box.addLayout(top)

        # 核心文案：从 expected_status → new_status（**服务端给的**，本地不重算）
        summary = QLabel(
            f"把「{self._app_name(active.get('application_id'))}」的投递状态从 "
            f"<b>{_status_label(active.get('expected_status'))}</b> 改为 "
            f"<b>{_status_label(active.get('new_status'))}</b>。")
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setWordWrap(True)
        box.addWidget(summary)
        box.addWidget(hint_label("以上「从 / 改为」是服务端给的，与真正会写入的内容一致。",
                                 shrink=True))

        if active.get("reason"):
            box.addWidget(QLabel("理由：" + active["reason"]))

        meta = QGridLayout()
        meta.setVerticalSpacing(3)
        meta.setHorizontalSpacing(10)
        meta.addWidget(QLabel("操作 ID"), 0, 0)
        mid = QLabel(active.get("action_id", ""))
        mid.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta.addWidget(mid, 0, 1)
        meta.addWidget(QLabel("工具"), 1, 0)
        meta.addWidget(QLabel(active.get("tool", "")), 1, 1)
        meta.addWidget(QLabel("过期时间"), 2, 0)
        meta.addWidget(QLabel(active.get("expires_at") or "—"), 2, 1)
        box.addLayout(meta)

        if active.get("status") == "pending_confirmation":
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            approve = primary_button("确认执行")
            approve.clicked.connect(lambda _checked=False, aid=active.get("action_id"):
                                    self._on_decide(aid, True))
            reject = QPushButton("取消，不要改")
            reject.setProperty("class", "ghost")
            reject.setCursor(Qt.CursorShape.PointingHandCursor)
            reject.clicked.connect(lambda _checked=False, aid=active.get("action_id"):
                                   self._on_decide(aid, False))
            btn_row.addWidget(approve)
            btn_row.addWidget(reject)
            btn_row.addWidget(QLabel("确认前不会改动任何业务数据；不确认也不会自动执行。"))
            btn_row.addStretch(1)
            box.addLayout(btn_row)
        else:
            box.addWidget(QLabel(
                f"这项操作已经处理过了（当前状态：{INTENT_STATUS_LABELS.get(active.get('status'), active.get('status'))}）。"
                "如果列表没刷新，请点下方「刷新待确认列表」。"))

        refresh = QPushButton("刷新待确认列表")
        refresh.setProperty("class", "ghost")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(lambda _c=False: self._refresh_pending())
        box.addWidget(refresh)

    def _active_intent(self) -> Optional[Dict[str, Any]]:
        """优先用刚跑完那轮的 pending_action，其次待确认列表第一条。两者都来自服务端。"""
        run = self._run or {}
        pa = run.get("pending_action")
        if isinstance(pa, dict):
            return pa
        if self._pending_actions:
            return self._pending_actions[0]
        return None

    def _render_wrote(self) -> None:
        run = self._run or {}
        if run.get("wrote_business_data"):
            self._wrote_card.show()
        else:
            self._wrote_card.hide()

    def _render_failure(self) -> None:
        run = self._run or {}
        wf = run.get("write_failure")
        if not isinstance(wf, dict):
            self._failure_card.hide()
            return
        self._failure_card.show()
        box = self._failure_box
        _clear(box)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(section_label("写操作没有完成"))
        top.addStretch(1)
        code = wf.get("code", "")
        top.addWidget(soft_chip(code, _C_RED))
        box.addLayout(top)
        if wf.get("message"):
            box.addWidget(QLabel(wf["message"]))
        hint = WRITE_FAILURE_HINTS.get(code)
        if hint:
            hl = QLabel(hint)
            hl.setProperty("class", "muted")
            hl.setWordWrap(True)
            box.addWidget(hl)
        else:
            box.addWidget(QLabel(f"服务端给出的失败码是 {code}，页面没有对应的解释文案（不猜）。"))

    def _render_evidence(self) -> None:
        run = self._run or {}
        out = run.get("output") or {}
        evs = out.get("evidence") or []
        if not run:
            self._evidence_card.hide()
            return
        self._evidence_card.show()
        box = self._evidence_box
        _clear(box)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(section_label(f"证据（可追溯）{('· ' + str(len(evs)) + ' 条') if evs else ''}"))
        box.addLayout(top)
        if not evs:
            box.addWidget(empty_state("本次运行没有引用证据", "没有证据支持的结论不会被展示为确定结论。"))
            return
        for e in evs:
            f = QFrame()
            f.setFrameShape(QFrame.StyledPanel)
            f.setStyleSheet("border:1px solid #e2e8f0; border-radius:10px; padding:8px 10px;")
            lay = QVBoxLayout(f)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(4)
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(8)
            ref = QLabel(e.get("ref", ""))
            ref.setStyleSheet("font-family: monospace; font-size:12px;")
            ref.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            head.addWidget(ref)
            head.addWidget(soft_chip(e.get("source", ""), _C_CYAN))
            if e.get("object_version"):
                head.addWidget(QLabel(e["object_version"]))
            head.addStretch(1)
            lay.addLayout(head)
            if e.get("summary"):
                lay.addWidget(QLabel(e["summary"]))
            box.addWidget(f)

    def _render_trace(self) -> None:
        box = self._trace_box
        _clear(box)
        trace = self._trace
        if not trace:
            box.addWidget(hint_label("点「刷新运行轨迹」查看该会话历次运行的 run_id 与工具调用。",
                                     shrink=True))
            return
        runs = trace.get("runs") or []
        if not runs:
            box.addWidget(empty_state("还没有运行记录"))
            return
        for t in runs:
            row = QHBoxLayout()
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(8)
            row.addWidget(soft_chip(RUN_STATUS_LABELS.get(t.get("status"), t.get("status")),
                                     _run_status_color(t.get("status"))))
            rid = QLabel(t.get("run_id", ""))
            rid.setStyleSheet("font-family: monospace; font-size:12px;")
            rid.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(rid)
            row.addWidget(QLabel(f"{len(t.get('tool_calls') or [])} 次工具调用"))
            row.addWidget(QLabel(f"{t.get('latency_ms', 0)} ms"))
            if t.get("decision"):
                row.addWidget(soft_chip(DECISION_LABELS.get(t["decision"], t["decision"]), _C_INDIGO))
            row.addStretch(1)
            box.addLayout(row)

    def _render_memory(self) -> None:
        proposed = [m for m in self._memories if m.get("status") == "proposed"]
        confirmed = [m for m in self._memories if m.get("status") == "confirmed"]
        _clear(self._proposed_box)
        _clear(self._confirmed_box)

        self._proposed_box.addWidget(QLabel(f"待你确认（{len(proposed)} 条）"))
        if not proposed:
            self._proposed_box.addWidget(QLabel("没有待确认的记忆。"))
        else:
            for m in proposed:
                self._proposed_box.addWidget(self._memory_card(m, confirmed=False))

        self._confirmed_box.addWidget(QLabel(f"已生效的记忆（{len(confirmed)} 条）"))
        if not confirmed:
            self._confirmed_box.addWidget(QLabel("还没有已确认的记忆。"))
        else:
            for m in confirmed:
                self._confirmed_box.addWidget(self._memory_card(m, confirmed=True))

    def _memory_card(self, m: Dict[str, Any], confirmed: bool) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.StyledPanel)
        if confirmed:
            f.setStyleSheet("border:1px solid #e2e8f0; border-radius:10px; padding:8px 10px;")
        else:
            f.setStyleSheet("border:1px solid #fde68a; background:#fffbeb; border-radius:10px; padding:8px 10px;")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(soft_chip(MEMORY_STATUS_LABELS.get(m.get("status"), m.get("status")),
                                 _C_AMBER if not confirmed else _C_GREEN))
        head.addWidget(soft_chip(MEMORY_TYPE_LABELS.get(m.get("memory_type"), m.get("memory_type")),
                                 _C_GRAY))
        head.addWidget(QLabel(m.get("memory_id", "")))
        if confirmed and m.get("source"):
            head.addWidget(QLabel(f"来源 {m['source']}"))
        head.addStretch(1)
        lay.addLayout(head)
        content = QLabel(m.get("content", ""))
        content.setWordWrap(True)
        lay.addWidget(content)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        if not confirmed:
            confirm = primary_button("记入记忆")
            confirm.clicked.connect(lambda _c=False, mid=m.get("memory_id"):
                                    self._on_confirm_memory(mid))
            btn_row.addWidget(confirm)
        delete = QPushButton("删除")
        delete.setProperty("class", "ghost")
        delete.setCursor(Qt.CursorShape.PointingHandCursor)
        delete.clicked.connect(lambda _c=False, mid=m.get("memory_id"):
                                self._on_delete_memory(mid))
        btn_row.addWidget(delete)
        btn_row.addStretch(1)
        # 记忆操作进行中禁用本卡按钮
        if self._memory_busy:
            for i in range(btn_row.count()):
                w = btn_row.itemAt(i).widget()
                if isinstance(w, QPushButton):
                    w.setEnabled(False)
        lay.addLayout(btn_row)
        return f

    @staticmethod
    def _selectable(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lbl

    # ===================== 刷新（切换页面 / 重连） =====================
    def refresh(self) -> None:
        """供主窗口切换页面 / 重连后调用。"""
        self._load_contexts()
        if self._session:
            sid = self._session.get("session_id")
            if sid:
                self._run_job(lambda: api.agent_get_session(sid), self._on_session_reloaded,
                              on_err=lambda _m: None)
            self._refresh_pending()
        self._refresh_memories()

    # _refresh 是主窗口重连时调用的名字（与 refresh 同义）
    _refresh = refresh

    def _on_pick_changed(self) -> None:
        self._picked_jd = self._jd_combo.currentData() or ""
        self._picked_resume = self._resume_combo.currentData() or ""
        self._render_selector()


def _chip_style(color: str) -> str:
    """给 soft_chip 同款半透明底 + 同色字（返回 QSS 字符串）。"""
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return (
        f"background: rgba({r},{g},{b},0.10); color: {color};"
        f"border: 1px solid rgba({r},{g},{b},0.35);"
        "border-radius: 10px; padding: 2px 10px; font-size: 12px;"
    )


class _BusyOverlay(QWidget):
    """整页加载遮罩（覆盖 AgentPage 视口）。"""

    def __init__(self, parent: QWidget, text: str = "Agent 运行中，请稍候…"):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: rgba(244,246,250,0.88);")
        layout = QVBoxLayout(self)
        self._label = QLabel(text)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("font-size: 15px; font-weight: 600; color: #6366f1;")
        layout.addStretch(1)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        self.hide()

    def show_over(self, text: Optional[str] = None) -> None:
        if text:
            self._label.setText(text)
        if self.parentWidget():
            self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()