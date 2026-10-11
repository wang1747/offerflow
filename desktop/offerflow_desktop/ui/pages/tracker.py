"""投递追踪页：九态看板 + 记录详情/事件时间线 + 统计 + 日历视图。"""

from datetime import date as _date

from PySide6.QtCore import QDate, QMimeData, Qt
from PySide6.QtGui import QColor, QDrag, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QCalendarWidget, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ... import api_client as db
from ...config import (
    APPLICATION_STATUSES, STATUS_COLORS, STATUS_COLORS_DARK, STATUS_LABELS,
)
from ...core import calendar_feed
from .. import charts, tokens
from ..components import (
    card, danger_button, hint_label, make_title, primary_button, section_label,
    set_prop, stat_card,
)

# 拖拽载荷用的私有 MIME 类型。看板之间只搬「投递 id」，不搬控件、也不让 Qt
# 自己挪条目 —— 原因见 :class:`KanbanColumn`。
KANBAN_MIME = "application/x-offerflow-application-id"

# 漏斗的阶段顺序（对齐网页版 StatsDashboard 的 funnelSteps）：
# 这是「一条投递会依次经过的状态」，与九态里的 rejected/accepted 这类
# **终局分支**不同 —— 把终局状态混进漏斗，条形就不再是漏斗了。
FUNNEL_STEPS = (
    "saved", "applied", "online_test", "first_interview",
    "second_interview", "hr_round", "offered",
)


def _num(value) -> str:
    """把统计数字格式化成人类可读的短串（``2`` / ``33.3``；坏值退回 ``0``）。"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "0"



def _kanban_app_id(item) -> str:
    """取出条目挂在 UserRole 上的投递 id（没有则返回空串）。"""
    value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
    return str(value) if value else ""


class KanbanColumn(QListWidget):
    """看板的一列：既是拖拽源，也是落点。

    为什么不用 Qt 自带的拖放搬运：默认实现只把条目从一个 list 挪到另一个 list
    的**视图**里，业务状态一动不动 —— 松手后看着换了列，刷新一次又跳回原列，
    用户以为「拖了没生效」。所以这里把落点语义定成「把这条投递改成我这一列的
    状态」：只从 MIME 里读投递 id 交给上层去改状态 + 整页刷新，自己一条条目
    都不动手。

    落点判据只看 MIME 格式，不看 ``event.source()``：
      · MIME 是本应用私有串，只有看板列会产出，判别力已经够；
      · 合成事件（测试里直接调 ``dropEvent``）拿不到 source，
        依赖它会让这条路径**没法被验证**。
    """

    def __init__(self, status: str, on_drop, parent=None):
        super().__init__(parent)
        self._status = status
        self._on_drop = on_drop
        self.setProperty("class", "kanban-list")
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(150)
        # 卡片之间的间隙。QSS 的 `::item` 只画边框/圆角，两张卡片会**贴在一起**
        # 分不出是一张还是两张；QListView.setSpacing 才是真的留缝。
        self.setSpacing(6)

    # ===== 拖出 =====

    def startDrag(self, supported_actions) -> None:   # noqa: N802 (Qt 虚函数名)
        app_id = _kanban_app_id(self.currentItem())
        if not app_id:
            return
        mime = QMimeData()
        mime.setData(KANBAN_MIME, app_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    # ===== 拖入 =====

    def _accepts(self, event) -> bool:
        return event.mimeData().hasFormat(KANBAN_MIME)

    def dragEnterEvent(self, event) -> None:          # noqa: N802
        if self._accepts(event):
            event.acceptProposedAction()
            set_prop(self, "dragover", "true")
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:           # noqa: N802
        if self._accepts(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:          # noqa: N802
        set_prop(self, "dragover", "false")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:               # noqa: N802
        if self._accepts(event):
            event.acceptProposedAction()
            set_prop(self, "dragover", "false")
            raw = bytes(event.mimeData().data(KANBAN_MIME))
            app_id = raw.decode("utf-8", "ignore")
            if app_id:
                self._on_drop(app_id, self._status)
            return
        super().dropEvent(event)


def _calendar_qss() -> str:
    """日历控件的内联样式（按当前主题二选一）。

    为什么不能靠全局 QSS：``QCalendarWidget`` 的内部件（导航栏 / 月视图 /
    翻月按钮）**不吃应用级 QSS**，深色主题下它就是一块白板，夹在深色界面里
    非常突兀。这里按主题给一份最小样式。
    """
    if tokens.is_dark():
        return """
QCalendarWidget QWidget { background: #23232b; color: #e6e6ee; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #2a2a34; }
QCalendarWidget QToolButton {
    color: #e6e6ee; background: transparent; border: none;
    padding: 4px 8px; border-radius: 6px;
}
QCalendarWidget QToolButton:hover { background: #34344a; }
QCalendarWidget QAbstractItemView {
    background: #1e1e26; color: #e6e6ee;
    selection-background-color: #4f46e5; selection-color: #ffffff;
    outline: none;
}
QCalendarWidget QAbstractItemView:disabled { color: #5b5b6b; }
"""
    return """
QCalendarWidget QWidget { background: #ffffff; color: #1f2937; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #f8fafc; }
QCalendarWidget QToolButton {
    color: #1f2937; background: transparent; border: none;
    padding: 4px 8px; border-radius: 6px;
}
QCalendarWidget QToolButton:hover { background: #e8eef7; }
QCalendarWidget QAbstractItemView {
    background: #ffffff; color: #1f2937;
    selection-background-color: #4f46e5; selection-color: #ffffff;
    outline: none;
}
QCalendarWidget QAbstractItemView:disabled { color: #c2c8d2; }
"""


class ApplicationDialog(QDialog):
    """新增投递记录。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增投递记录")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.company = QLineEdit(placeholderText="公司名称")
        self.position = QLineEdit(placeholderText="岗位名称")
        self.jd_combo = QComboBox()
        self.jd_combo.addItem("（不关联）", "")
        for j in db.list_jds():
            company = ((j.get("company") or "?").replace("\n", " ").strip())[:8]
            position = ((j.get("position") or "?").replace("\n", " ").strip())[:10]
            self.jd_combo.addItem(f"{company} · {position}", j["id"])
        self.resume_combo = QComboBox()
        self.resume_combo.addItem("（不关联）", "")
        for r in db.list_resumes():
            label = (r.get("parsed_json") or {}).get("position") or r["raw_text"][:20]
            label = (label or "").replace("\n", " ").replace("\r", " ").strip()
            self.resume_combo.addItem(label[:16] if label else "（未命名简历）", r["id"])
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("备注（渠道、内推人、投递链接等）")
        self.notes.setFixedHeight(70)
        form.addRow("公司*", self.company)
        form.addRow("岗位", self.position)
        form.addRow("关联岗位 JD", self.jd_combo)
        form.addRow("使用简历", self.resume_combo)
        form.addRow("备注", self.notes)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self):
        if not self.company.text().strip():
            QMessageBox.warning(self, "缺少信息", "请填写公司名称")
            return
        self.accept()


class EditDialog(QDialog):
    """编辑投递记录。"""

    def __init__(self, app_data: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑 · {app_data['company']}")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        self.status = QComboBox()
        for s in APPLICATION_STATUSES:
            self.status.addItem(STATUS_LABELS[s], s)
        idx = APPLICATION_STATUSES.index(app_data["status"]) if app_data["status"] in APPLICATION_STATUSES else 0
        self.status.setCurrentIndex(idx)
        self.applied_date = QDateEdit()
        self.applied_date.setCalendarPopup(True)
        self.applied_date.setDisplayFormat("yyyy-MM-dd")
        self.applied_date.setSpecialValueText("（未设置）")
        from PySide6.QtCore import QDate
        self.applied_date.setMinimumDate(QDate(2000, 1, 1))
        if app_data.get("applied_date"):
            self.applied_date.setDate(QDate.fromString(app_data["applied_date"], "yyyy-MM-dd"))
        else:
            self.applied_date.setDate(QDate.currentDate())
        self.next_action = QLineEdit(app_data.get("next_action") or "")
        self.next_action.setPlaceholderText("如：二面 / 跟进 HR / 准备笔试")
        self.next_action_date = QDateEdit()
        self.next_action_date.setCalendarPopup(True)
        self.next_action_date.setDisplayFormat("yyyy-MM-dd")
        if app_data.get("next_action_date"):
            self.next_action_date.setDate(QDate.fromString(app_data["next_action_date"], "yyyy-MM-dd"))
        else:
            self.next_action_date.setDate(QDate.currentDate())
        self.notes = QPlainTextEdit(app_data.get("notes") or "")
        self.notes.setFixedHeight(70)
        form.addRow("状态", self.status)
        form.addRow("投递日期", self.applied_date)
        form.addRow("下一步行动", self.next_action)
        form.addRow("行动日期", self.next_action_date)
        form.addRow("备注", self.notes)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class TrackerPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        # 日历视图的状态。**必须在建 Tab 之前初始化** —— `_build_calendar_tab`
        # 里会立刻回调一次刷新，那时若字段还不存在就是 AttributeError
        # （这是错题本里「构造 ≠ 初始化」的另一种形态：初始化顺序也是构造的一部分）。
        self._feed: list = []
        self._marked_dates: list = []
        self._cal_error = ""
        # **表格里那一份**投递列表。表格、选中行、日历、导出全部从它取 ——
        # 行号只有在「同一份列表」里才有意义（详见 `_filtered_apps`）。
        self._apps: list = []
        # 看板的九个列控件（拖拽落点）。每次 `_refresh` 重建，这里只留引用，
        # 供落点回调 / 测试取用；**列内容一律从 `_apps` 现算**，不另存一份。
        self._columns: list = []
        # 统计 Tab 的状态。**构造期不拉数据**（构造只搭界面这条硬规矩：
        # 在 __init__ 里发请求，logout→relogin 重建窗口时会卡住）。
        # 首次显示或切到该 Tab 时才加载；`_refresh()` 只把它标脏。
        self._stats: dict = {}
        self._stats_error = ""
        self._stats_dirty = True
        self._stats_cards: dict = {}
        self._stats_page = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "投递追踪",
            "看板管理每份投递：九态流转、事件时间线、转化率统计、待办提醒。",
        ))

        top = QHBoxLayout()
        add_btn = primary_button("+ 新增投递")
        add_btn.clicked.connect(self._add_application)
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部状态", "")
        for s in APPLICATION_STATUSES:
            self.filter_combo.addItem(STATUS_LABELS[s], s)
        self.filter_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(add_btn)
        top.addWidget(self.filter_combo)
        top.addStretch(1)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ===== 看板视图 =====
        board_scroll = QScrollArea()
        board_scroll.setWidgetResizable(True)
        board_host = QWidget()
        self.board_layout = QHBoxLayout(board_host)
        self.board_layout.setContentsMargins(0, 0, 0, 0)
        self.board_layout.setSpacing(8)
        board_scroll.setWidget(board_host)
        board_panel = card()
        bp = QVBoxLayout(board_panel)
        bp.setContentsMargins(12, 10, 12, 10)
        board_head = QHBoxLayout()
        board_head.addWidget(section_label("投递看板"))
        # 拖拽是**隐式**操作：不写一句，用户不会去试着拖一张卡片。
        board_head.addWidget(hint_label("拖拽卡片到目标列即可改状态"))
        board_head.addStretch(1)
        bp.addLayout(board_head)
        bp.addWidget(board_scroll, 1)
        board_panel.setMinimumHeight(280)
        splitter.addWidget(board_panel)

        # ===== 列表 + 详情 =====
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 6, 0, 0)

        list_panel = card()
        lp = QVBoxLayout(list_panel)
        lp.setContentsMargins(12, 10, 12, 10)
        lp.addWidget(section_label("投递列表"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["公司", "岗位", "状态", "更新时间"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemDoubleClicked.connect(lambda _: self._edit_application())
        lp.addWidget(self.table)
        btn_row = QHBoxLayout()
        edit_btn = QPushButton("编辑")
        edit_btn.clicked.connect(self._edit_application)
        del_btn = danger_button("删除")
        del_btn.clicked.connect(self._delete_application)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        agent_btn = QPushButton("用 Agent 制定行动")
        agent_btn.clicked.connect(self._open_agent_for_app)
        btn_row.addWidget(agent_btn)
        btn_row.addStretch(1)
        lp.addLayout(btn_row)
        bottom_layout.addWidget(list_panel, 3)

        detail_panel = card()
        dp = QVBoxLayout(detail_panel)
        dp.setContentsMargins(12, 10, 12, 10)
        dp.addWidget(section_label("事件时间线 / 统计"))
        self.detail_view = QLabel("双击左侧记录查看事件时间线与转化统计")
        self.detail_view.setWordWrap(True)
        self.detail_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dp.addWidget(self.detail_view, 1)
        bottom_layout.addWidget(detail_panel, 2)

        splitter.addWidget(bottom)
        splitter.setSizes([300, 280])

        # ===== 视图切换：看板/列表 与 日历 =====
        # 用 Tab 而不是并排：日历需要吃掉整块高度，和看板挤在一起两边都难看。
        self.tabs = QTabWidget()
        self.tabs.addTab(splitter, "看板与列表")
        self.tabs.addTab(self._build_calendar_tab(), "日历")
        self._stats_page = self._build_stats_tab()
        self.tabs.addTab(self._stats_page, "统计")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        self._refresh()

    # ===== 日历视图（构建）=====

    def _build_calendar_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(tokens.SP_SM)

        bar = QHBoxLayout()
        self.cal_scope = QLabel("")
        self.cal_scope.setObjectName("calScopeLabel")
        bar.addWidget(self.cal_scope)
        bar.addStretch(1)
        self.cal_include_applied = QCheckBox("连投递日一起导出／落格")
        self.cal_include_applied.setToolTip(
            "不勾选时，日历只显示「下一步行动／面试」事件。\n"
            "投递日几乎每天都有，全铺上去会把真正要看的面试淹没。")
        self.cal_include_applied.setChecked(False)
        self.cal_include_applied.stateChanged.connect(lambda _s: self._refresh_calendar())
        bar.addWidget(self.cal_include_applied)
        self.cal_export_btn = primary_button("导出日历 (.ics)")
        self.cal_export_btn.setToolTip("导出成通用日历格式，可导入手机／系统日历／Google Calendar")
        self.cal_export_btn.clicked.connect(self._export_ics)
        bar.addWidget(self.cal_export_btn)
        root.addLayout(bar)

        body = QHBoxLayout()
        body.setSpacing(tokens.SP_MD)

        left = card()
        lp = QVBoxLayout(left)
        lp.setContentsMargins(*tokens.CARD_MARGINS_SM)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.setDateTextFormat(QDate(), QTextCharFormat())
        self.calendar.selectionChanged.connect(self._on_calendar_day)
        # 翻月也要重打记号：QCalendarWidget 只保留「当前可见月份」的字符格式，
        # 不翻月刷新的话，翻回去记号就没了（不是记错了，是根本没画）。
        self.calendar.currentPageChanged.connect(lambda *_: self._refresh_calendar_marks())
        self.calendar.setStyleSheet(_calendar_qss())
        lp.addWidget(self.calendar, 1)
        body.addWidget(left, 3)

        right = card()
        rp = QVBoxLayout(right)
        rp.setContentsMargins(*tokens.CARD_MARGINS_SM)
        rp.addWidget(section_label("当日日程"))
        self.cal_day_label = QLabel("")
        self.cal_day_label.setObjectName("calDayLabel")
        self.cal_day_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        rp.addWidget(self.cal_day_label)
        self.cal_list = QListWidget()
        self.cal_list.setAlternatingRowColors(False)
        self.cal_list.setWordWrap(True)
        self.cal_list.itemDoubleClicked.connect(self._on_calendar_item)
        rp.addWidget(self.cal_list, 1)
        self.cal_hint = QLabel("双击条目可跳到该投递")
        self.cal_hint.setObjectName("calHintLabel")
        rp.addWidget(self.cal_hint)
        body.addWidget(right, 2)

        root.addLayout(body, 1)
        return page

    # ===== 统计视图（构建）=====

    def _build_stats_tab(self) -> QWidget:
        """投递统计：四张概览卡 + 投递漏斗 + 状态分布。

        数据来自服务端 ``/applications/stats``，口径是**全部投递**，
        与本页顶部的状态筛选无关 —— 「只筛着已拒绝看转化率」没有意义，
        所以统计不跟着 ``filter_combo`` 走（这是个容易搞错的地方：
        直接拿 ``self._apps`` 算会得到被筛过的数字）。
        """
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(tokens.SP_MD)

        bar = QHBoxLayout()
        self.stats_scope = QLabel("")
        self.stats_scope.setObjectName("statsScopeLabel")
        bar.addWidget(self.stats_scope)
        bar.addStretch(1)
        self.stats_refresh_btn = primary_button("刷新统计")
        self.stats_refresh_btn.setToolTip("重新向服务端取一次全量投递统计")
        self.stats_refresh_btn.clicked.connect(lambda: self._load_stats())
        bar.addWidget(self.stats_refresh_btn)
        root.addLayout(bar)

        grid = QGridLayout()
        grid.setSpacing(tokens.SP_MD)
        specs = (
            ("total", "投递总数", "#4f46e5", "全流程追踪"),
            ("applied_to_interview", "投递 → 面试率", "#2563eb", "转化"),
            ("interview_to_offer", "面试 → Offer 率", "#16a34a", "转化"),
            ("accepted", "已接受", "#0891b2", "拿到并接受"),
        )
        self._stats_cards = {}
        for i, (key, title, accent, sub) in enumerate(specs):
            tile = stat_card(title, "—", accent, sub)
            # 记下 key → 卡片，刷新时**原地改数字**，不重建卡片
            # （重建会丢掉网格里的位置，界面还会闪一下）。
            self._stats_cards[key] = tile
            grid.addWidget(tile, 0, i)
        root.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(tokens.SP_MD)

        funnel_panel = card()
        fp = QVBoxLayout(funnel_panel)
        fp.setContentsMargins(*tokens.CARD_MARGINS)
        fp.setSpacing(tokens.SP_SM)
        fp.addWidget(section_label("投递漏斗"))
        fp.addWidget(hint_label("从收藏到 Offer 逐阶段的人数，看卡在哪一步"))
        self.funnel_chart = charts.BarChart(
            "还没有投递记录，添加后这里会显示各阶段漏斗")
        # 两侧卡片等高，漏斗行数却少 —— 前后各留一段，图表居中而不是顶到上头
        fp.addStretch(1)
        fp.addWidget(self.funnel_chart)
        fp.addStretch(1)
        row.addWidget(funnel_panel, 1)

        donut_panel = card()
        dp = QVBoxLayout(donut_panel)
        dp.setContentsMargins(*tokens.CARD_MARGINS)
        dp.setSpacing(tokens.SP_SM)
        dp.addWidget(section_label("状态分布"))
        dp.addWidget(hint_label("各状态占比（值为 0 的状态不显示）"))
        self.status_chart = charts.DonutChart(
            "还没有投递记录，添加后这里会显示各状态占比")
        dp.addWidget(self.status_chart, 1)
        row.addWidget(donut_panel, 1)

        root.addLayout(row, 1)
        return page

    # ===== 统计视图（刷新）=====

    def _on_tab_changed(self, _index: int) -> None:
        """切到统计 Tab 时按需加载：没人看就不打这个网络请求。"""
        if self._stats_page is not None and \
                self.tabs.currentWidget() is self._stats_page and self._stats_dirty:
            self._load_stats()

    def showEvent(self, event) -> None:                  # noqa: N802
        super().showEvent(event)
        # 页面重新显示时，若期间有过写操作（`_refresh` 标了脏）就补一次统计。
        if self._stats_dirty and self._stats_page is not None:
            self._load_stats()

    def _load_stats(self) -> None:
        """取全量投递统计并刷新统计页。

        失败/离线**不影响**看板、列表、日历：统计是附加视图，取不到就显示
        空态并如实说明原因，不该把整页拖下水。
        """
        self._stats_dirty = False
        try:
            data = db.get_statistics()
            self._stats = data if isinstance(data, dict) else {}
            self._stats_error = ""
        except Exception as exc:                          # noqa: BLE001
            self._stats = {}
            self._stats_error = str(exc)
        self._render_stats()

    def _render_stats(self) -> None:
        stats = self._stats or {}
        counts = stats.get("status_counts")
        counts = counts if isinstance(counts, dict) else {}
        conv = stats.get("conversion_rate")
        conv = conv if isinstance(conv, dict) else {}

        if self._stats_error:
            self.stats_scope.setText(f"统计加载失败：{self._stats_error}")
        elif not stats:
            self.stats_scope.setText("暂未取到统计（离线时展示的是上次同步的副本）")
        else:
            self.stats_scope.setText("口径：全部投递，不受上方状态筛选影响")

        values = {
            "total": _num(stats.get("total", 0)),
            "applied_to_interview": _num(conv.get("applied_to_interview", 0)) + "%",
            "interview_to_offer": _num(conv.get("interview_to_offer", 0)) + "%",
            "accepted": _num(counts.get("accepted", 0)),
        }
        for key, tile in self._stats_cards.items():
            tile.value_label.setText(values.get(key, "—"))

        colors = STATUS_COLORS_DARK if tokens.is_dark() else STATUS_COLORS

        def color_of(status: str) -> str:
            return colors.get(status, "#9ca3af" if tokens.is_dark() else "#94a3b8")

        self.funnel_chart.set_items([
            (STATUS_LABELS.get(k, k), counts.get(k, 0), color_of(k))
            for k in FUNNEL_STEPS
        ])
        order = stats.get("status_order")
        if not isinstance(order, list) or not order:
            order = list(APPLICATION_STATUSES)
        self.status_chart.set_items([
            (STATUS_LABELS.get(k, k), counts.get(k, 0), color_of(k))
            for k in order
        ])

    # ===== 日历视图（刷新）=====

    def _load_apps(self) -> list:
        """**唯一的读取点**：按当前状态筛选取一份投递列表。

        为什么把「读」和「用」分开：越权读取的地方越多，「表格里显示的」
        与「按行号取到的」就越容易不是同一份列表 —— 见 :meth:`_filtered_apps`。
        """
        status_filter = self.filter_combo.currentData() if hasattr(self, "filter_combo") else ""
        return [a for a in db.list_applications()
                if not status_filter or a.get("status") == status_filter]

    def _filtered_apps(self) -> list:
        """**表格里那一份**列表（缓存，不重读）。

        这是个真实的坑，不是洁癖：原实现里 `_refresh` 读一份填表格，而
        `_selected_app` 又**重新读一份**再按行号取。两边顺序只要有一点不同
        （后台 30 分钟同步刚落地、并发删了一条），用户「选中的是张三、
        删掉的却是李四」。行号只在同一份列表里才有意义，所以行号的使用者
        必须共用同一份数据。
        """
        return self._apps

    def _refresh_calendar(self, apps=None) -> None:
        """按给定（或缓存的）投递列表重算日程、重打记号、重画当日列表。"""
        if not hasattr(self, "cal_list"):     # 日历 Tab 还没建起来（构造中途）
            return
        include_applied = self.cal_include_applied.isChecked()
        # 错误信息存成状态，**不能只在这里 setText** —— 下面紧接着要调
        # `_on_calendar_day()` 刷当日列表，它会顺手把标签改写成「无安排」，
        # 于是失败提示只活了一个语句、用户一个字都看不到。
        self._cal_error = ""
        source = self._apps if apps is None else apps
        try:
            self._feed = calendar_feed.collect_events(
                source, include_applied=include_applied)
        except Exception as exc:                      # noqa: BLE001
            # 日历是**附加视图**，它坏掉不该把整个页面拖下水（看板/列表还得好用）。
            self._feed = []
            self._cal_error = str(exc)
        self._refresh_calendar_marks()
        self._on_calendar_day()

    def _refresh_calendar_marks(self) -> None:
        """给有日程的日期加粗上色。

        先清掉上一轮打过的记号：``setDateTextFormat`` 是**累积**的，
        不清的话筛掉的状态还会留着颜色，看上去「日历和列表对不上」。
        """
        for day in self._marked_dates:
            self.calendar.setDateTextFormat(day, QTextCharFormat())
        self._marked_dates = []
        if not self._feed:
            return
        accent = "#60a5fa" if tokens.is_dark() else "#1d4ed8"
        busy = QTextCharFormat()
        busy.setFontWeight(QFont.Weight.Bold)
        busy.setForeground(QColor(accent))
        for day in sorted({ev.day for ev in self._feed}):
            qdate = QDate(day.year, day.month, day.day)
            self.calendar.setDateTextFormat(qdate, busy)
            self._marked_dates.append(qdate)

    def _on_calendar_day(self) -> None:
        selected = self.calendar.selectedDate()
        # 不走 QDate.toPython()：不同 PySide6 小版本上时有时无，
        # 而这里只是取个年月日，直接构造是最稳的。
        day = _date(selected.year(), selected.month(), selected.day())
        if self._cal_error:
            # 取数失败时不要把标签刷成「无安排」——「读不到」和「确实没有」
            # 是两件事，混在一起用户会以为自己的投递都丢了。
            self.cal_list.clear()
            self.cal_hint.setText("")
            self.cal_day_label.setText(f"日程读取失败：{self._cal_error}")
            return
        events = [ev for ev in self._feed if ev.day == day]
        self.cal_list.clear()
        self.cal_day_label.setText(
            f"{day.isoformat()}　{len(events)} 项" if events else f"{day.isoformat()}　无安排")
        for ev in events:
            stamp = ev.at.strftime("%H:%M") if ev.at else "全天"
            item = QListWidgetItem(f"{stamp}　{ev.summary}")
            if ev.detail:
                item.setToolTip(ev.detail)
            # 业务键挂在条目上，双击时不用再去猜是哪条投递。
            item.setData(Qt.ItemDataRole.UserRole, ev.app_id)
            self.cal_list.addItem(item)
        self.cal_hint.setText("双击条目可跳到该投递" if events else "")

    def _on_calendar_item(self, item: QListWidgetItem) -> None:
        app_id = item.data(Qt.ItemDataRole.UserRole)
        if not app_id:
            return
        # 选中列表里对应那一行：用户从日历跳过来，是为了接着编辑／删掉它。
        apps = self._filtered_apps()
        for row, app in enumerate(apps):
            if app.get("id") == app_id:
                self.table.selectRow(row)
                break
        self.tabs.setCurrentIndex(0)

    # ===== 导出 .ics =====

    def _export_ics(self) -> None:
        if not self._feed:
            QMessageBox.information(
                self, "没有可导出的日程",
                "当前筛选下没有带日期的事件。\n\n"
                "请在「编辑投递」里填写「下一步行动 + 行动日期」，"
                "或勾选上方的「连投递日一起导出」。")
            return
        default_name = f"求职日程-{QDate.currentDate().toString('yyyyMMdd')}.ics"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日历", default_name, "日历文件 (*.ics)")
        if not path:
            return
        if not path.lower().endswith(".ics"):
            path += ".ics"
        try:
            # 导「眼前这份」：`self._feed` 就是日历正在显示的东西。
            # 重新读一遍数据会让文件与界面不一致，而且用户毫无察觉。
            text = calendar_feed.build_ics_from_feed(self._feed)
            # ⚠ 必须写字节。RFC5545 要求 CRLF 行尾，而 Python 文本模式在 Windows
            # 会把 \n 翻成 \r\n、把已有的 \r\n 再翻成 \r\r\n —— 直接写文本，
            # 导出的文件在 Outlook 里就是「每行后面多一个空行」，还会解析失败。
            with open(path, "wb") as fh:
                fh.write(text.encode("utf-8"))
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"写文件失败：{exc}")
            return
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(self, "导出失败", f"生成日历时出错：{exc}")
            return
        QMessageBox.information(
            self, "已导出",
            f"已写入 {len(self._feed)} 条日程：\n{path}\n\n"
            "导入方式：手机／系统日历「导入日历文件」，或直接打开该文件。\n"
            "重复导入同一份不会产生重复事件（每条事件的 ID 是固定的）。")

    # ===== 渲染 =====

    def _refresh(self) -> None:
        apps = self._load_apps()
        self._apps = apps

        self._refresh_board(apps)

        # 列表
        self.table.setRowCount(len(apps))
        for i, a in enumerate(apps):
            self.table.setItem(i, 0, QTableWidgetItem(a["company"]))
            self.table.setItem(i, 1, QTableWidgetItem(a.get("position") or ""))
            status_item = QTableWidgetItem(STATUS_LABELS.get(a["status"], a["status"]))
            status_item.setForeground(Qt.GlobalColor.white)
            from PySide6.QtGui import QColor
            status_item.setBackground(QColor(STATUS_COLORS.get(a["status"], "#94a3b8")))
            self.table.setItem(i, 2, status_item)
            self.table.setItem(i, 3, QTableWidgetItem((a.get("updated_at") or "")[:16]))
        # 传进去而不是让它自己去读：**日历必须与表格同源**，否则一次后台同步
        # 就能让两边对不上（而且要多打一次网络请求）。
        self._refresh_calendar(apps)
        self.mw.refresh_status()

        # 任何写操作都会走到这里，统计口径随之变化 → 标脏。用户正停在统计
        # Tab 上就当场重取（否则他要手动点刷新才看到新数字）；否则等他切过去。
        self._stats_dirty = True
        if self._stats_page is not None and \
                self.tabs.currentWidget() is self._stats_page:
            self._load_stats()

    # ===== 看板（九态分列 + 拖拽改状态）=====

    def _refresh_board(self, apps: list) -> None:
        """重建九态看板。

        列内容一律**从传进来的 `apps` 现算**（和表格、日历同一份），
        不在列控件里另存一份数据 —— 否则拖拽改状态后会出现「列里是新状态、
        表格里是旧状态」这种自相矛盾。
        """
        while self.board_layout.count():
            item = self.board_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._columns = []
        for status in APPLICATION_STATUSES:
            cards = [a for a in apps if a.get("status") == status]
            self.board_layout.addWidget(self._build_column(status, cards), 1)

    def _build_column(self, status: str, cards: list) -> QWidget:
        column = QFrame()
        column.setProperty("class", "kanban-col")
        cl = QVBoxLayout(column)
        cl.setContentsMargins(*tokens.CARD_MARGINS_SM)
        cl.setSpacing(tokens.SP_SM)

        color = STATUS_COLORS.get(status, "#94a3b8")
        header = QLabel(f"{STATUS_LABELS.get(status, status)} · {len(cards)}")
        header.setStyleSheet(
            f"font-weight:700; color:{color}; background:transparent; border:none;")
        cl.addWidget(header)

        column_list = KanbanColumn(status, self._on_kanban_drop)
        column_list.setToolTip("拖拽卡片到这一列，即把该投递改成「%s」"
                               % STATUS_LABELS.get(status, status))
        for app in cards:
            column_list.addItem(self._card_item(app))
        column_list.itemDoubleClicked.connect(self._on_card_activated)

        # 空列提示用**独立标签**而不是占位条目：占位条目能被选中、能被当作
        # 拖拽源、还会把「这一列有几条投递」算错一位。
        # 位置在**表头正下方**（列表之前）：跟在列表后面会被列表的 stretch
        # 顶到列底，离表头一整屏远，看着像「这一列坏了」。
        empty = hint_label("暂无投递", shrink=True)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setVisible(not cards)
        cl.addWidget(empty)
        cl.addWidget(column_list, 1)
        self._columns.append(column_list)
        return column

    def _card_item(self, app: dict) -> QListWidgetItem:
        company = (app.get("company") or "").strip() or "（未填公司）"
        position = (app.get("position") or "").strip() or "—"
        item = QListWidgetItem(f"{company}\n{position}")
        item.setData(Qt.ItemDataRole.UserRole, app.get("id"))
        tips = []
        if app.get("next_action"):
            tips.append(f"下一步：{app['next_action']}")
        if app.get("next_action_date"):
            tips.append(f"行动日期：{app['next_action_date']}")
        if app.get("notes"):
            tips.append(str(app["notes"]).strip())
        # 看板卡片只有两行，剩下的信息靠 tooltip —— 但拖拽提示**必须**在，
        # 否则用户不知道这张卡片能拖。
        tips.append("拖到目标列即可改状态；双击编辑详情")
        item.setToolTip("\n".join(tips))
        return item

    def _on_kanban_drop(self, app_id: str, new_status: str) -> bool:
        """看板拖拽落点：把 ``app_id`` 那条投递改成 ``new_status``。

        有两处必须自己拦下来（服务端只校验「状态属于枚举」，**没有流转状态机**，
        所以这里也不发明规则）：
          · 目标状态不在枚举里 —— 没必要发一个注定 400 的请求，
            本地先写后回滚还会多闪一次警告；
          · 拖回原列 —— 状态没变，纯噪声（手抖拖到自己那列很常见）。
        两种情况都返回 False，且**一次网络都不碰**。

        返回值是给内部用的布尔（是否真的落库了），UI 只吃 ``_write`` 的提示。
        """
        if new_status not in APPLICATION_STATUSES:
            return False
        app = next((a for a in self._apps if a.get("id") == app_id), None)
        if app is None or app.get("status") == new_status:
            return False
        if not self._write(
                "更新状态",
                lambda: db.update_application(app_id, status=new_status)):
            return False
        self._refresh()
        return True

    def _on_card_activated(self, item: QListWidgetItem) -> None:
        """双击看板卡片 = 选中列表里那一条并打开编辑（对齐网页版的「编辑详情」）。

        「先选中再编辑」不是多余动作：`_edit_application` 走的是
        `_selected_app()`（按表格当前行取），不先同步选中行就会编辑到别人。
        """
        app_id = _kanban_app_id(item)
        if not app_id:
            return
        for row, app in enumerate(self._filtered_apps()):
            if app.get("id") == app_id:
                self.table.selectRow(row)
                self._edit_application()
                return

    # ===== 操作 =====

    def _write(self, action: str, fn) -> bool:
        """执行写操作并统一处理失败，返回是否成功。

        为什么必须有这一层：这些接口原先在 api_client 里 `except ApiError: pass`，
        断网时「删除成功」是**假的**（服务器上还在，刷新一次又出现）。
        现在异常会抛到这里，必须接住并如实告知用户。

        P4 之后投递的增删改都是「本地先落 + 排队上行」，正常情况下不会再走到
        离线分支；真走到这里说明该动作仍是联网独占（如投递事件 / 提醒）。
        """
        try:
            fn()
            return True
        except db.ApiOfflineError as exc:
            QMessageBox.warning(
                self, "当前离线",
                f"{exc}\n\n这个操作需要联网后重试；其余改动已存在本地，联网后会自动同步。")
            return False
        except db.ApiError as exc:
            QMessageBox.warning(self, f"{action}失败", str(exc))
            return False

    def _add_application(self) -> None:
        dlg = ApplicationDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        def _do() -> None:
            db.create_application(
                dlg.company.text().strip(), dlg.position.text().strip(),
                dlg.jd_combo.currentData() or None, dlg.resume_combo.currentData() or None)
            if dlg.notes.toPlainText().strip():
                apps = db.list_applications()
                if apps:
                    db.add_application_event(apps[0]["id"], dlg.notes.toPlainText().strip())

        if self._write("新增投递", _do):
            self._refresh()

    def _selected_app(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        apps = self._filtered_apps()
        return apps[row] if row < len(apps) else None

    def _open_agent_for_app(self) -> None:
        """从投递追踪带着当前投递进入求职 Agent（审计 P1-2）。"""
        app = self._selected_app()
        if not app:
            QMessageBox.information(self, "提示", "请先在列表中选择一条投递")
            return
        entry = {"application_id": app["id"], "entry_source": "tracker"}
        if app.get("jd_id"):
            entry["jd_id"] = app["jd_id"]
        if app.get("resume_id"):
            entry["resume_id"] = app["resume_id"]
        self.mw.open_agent(entry)

    def _edit_application(self) -> None:
        app = self._selected_app()
        if not app:
            QMessageBox.information(self, "提示", "请先在列表中选择一条投递")
            return
        dlg = EditDialog(app, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            applied = dlg.applied_date.date().toString("yyyy-MM-dd")

            def _do() -> None:
                db.update_application(
                    app["id"], status=dlg.status.currentData(),
                    applied_date=applied if app.get("applied_date") or dlg.applied_date.date() != dlg.applied_date.date().currentDate() else None,
                    next_action=dlg.next_action.text().strip(),
                    next_action_date=dlg.next_action_date.date().toString("yyyy-MM-dd"),
                    notes=dlg.notes.toPlainText())

            if self._write("保存投递", _do):
                self._refresh()
                self._show_timeline(app["id"])

    def _delete_application(self) -> None:
        app = self._selected_app()
        if not app:
            QMessageBox.information(self, "提示", "请先在列表中选择一条投递")
            return
        if QMessageBox.question(
            self, "删除确认", f"确定删除「{app['company']} · {app['position']}」的投递记录？"
        ) == QMessageBox.StandardButton.Yes:
            if self._write("删除投递", lambda: db.delete_application(app["id"])):
                self._refresh()

    def _show_timeline(self, app_id: str) -> None:
        events = db.list_application_events(app_id)
        stats = db.get_statistics()
        conv = stats["conversion_rate"]
        lines = ["【事件时间线（最新在前）】"]
        if events:
            for e in events:
                lines.append(f"· {(e.get('event_date') or '')[:16]}  {e.get('description') or e.get('event_type')}")
        else:
            lines.append("· 暂无事件")
        lines.append("")
        lines.append("【转化统计】")
        lines.append(f"· 投递总数：{stats['total']}")
        lines.append(f"· 投递 → 面试：{conv['applied_to_interview']}%")
        lines.append(f"· 面试 → Offer：{conv['interview_to_offer']}%")
        self.detail_view.setText("\n".join(lines))
