"""管理后台：把网页版管理区的 8 个模块搬进桌面版。

模块（与网页版 admin 区一一对应）：
    管理总览 / 用户管理 / 用量与成本 / 内容治理 / 系统健康 / 审计日志 / 系统设置 / 账号安全

入口只在账号 `role == admin` 时出现在侧栏（main_window 负责判断）。
所有接口走 `/api/v1/admin/*`，服务端 require_role 把关；接口不可用时会把原因
回显在页面上（`_error` 字段），而不是显示成「空白无数据」。
"""

from typing import Any, Dict, List, Optional
import ast
import json
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ... import api_client as api
from ... import database as db
from ...llm import LLMWorker
from ..components import api_guard, card, hint_label, make_title, section_label, stat_card
from ..profile_dialogs import ChangePasswordDialog
from .cost import OPERATION_LABELS, fmt_duration


def _async(owner, fn, on_ok, on_fail=None) -> LLMWorker:
    """统一异步调用：持有 worker 引用防 GC，跨线程回主线程走信号。

    `on_ok` 外面包了一层异常保护：渲染期抛异常时 PySide6 只会把 traceback
    打到 stderr，界面表现为「区块凭空消失、也没有任何提示」，很难发现
    （v2.2.7 就漏过一个「%， 被当成格式符」的 ValueError）。这里打印堆栈
    并通过 on_fail 给出可见提示。
    """
    fail = on_fail or (lambda _m: None)

    def _safe_ok(result):
        try:
            on_ok(result)
        except Exception as exc:                    # noqa: BLE001
            traceback.print_exc()
            fail(f"渲染失败：{exc}")

    worker = LLMWorker(api_guard(fn))
    worker.success.connect(_safe_ok)
    worker.failed.connect(fail)
    worker.start()
    owner._worker = worker
    return worker


def _fmt_time(raw: str) -> str:
    """ISO(UTC) → 北京时间 MM-DD HH:MM。"""
    text = str(raw or "")
    if "T" not in text:
        return text[:16] or "—"
    try:
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(text) + timedelta(hours=8)).strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16]


def _fmt_cost(v: Any) -> str:
    """人民币格式化：不足 1 元保留 4 位小数，否则保留 2 位。"""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        f = 0.0
    if not f:
        return "¥0.00"
    return f"¥{f:.4f}" if f < 1 else f"¥{f:.2f}"


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()


def _err_banner(payload: Dict[str, Any]) -> Optional[QLabel]:
    """管理接口失败时返回一条醒目提示，否则 None。"""
    err = (payload or {}).get("_error")
    if not err:
        return None
    label = QLabel(f"⚠ {err}")
    label.setWordWrap(True)
    label.setStyleSheet(
        "background: rgba(220,38,38,0.10); color: #dc2626;"
        "border: 1px solid rgba(220,38,38,0.30); border-radius: 8px;"
        "padding: 8px 12px; font-size: 12px;")
    return label


def _table(headers: List[str], stretch_col: int = 1) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setShowGrid(False)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setDefaultSectionSize(30)
    header = t.horizontalHeader()
    # 空表时 ResizeToContents 会把各列压到最窄、挤成一团，给个下限保证表头舒展
    header.setMinimumSectionSize(72)
    for i in range(len(headers)):
        header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    if 0 <= stretch_col < len(headers):
        header.setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
    return t


def _empty_hint(text: str) -> QLabel:
    """表格为空时的一行说明（避免出现「一片白、没有任何解释」）。"""
    label = QLabel(text)
    label.setProperty("class", "hint")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("padding: 6px 0;")
    return label


def _fmt_detail(raw: Any) -> str:
    """审计详情：把 `{'字段': 'quota_daily_tokens', '旧值': 0, '新值': 50000}`
    这种人机都不友好的 dict repr，变成「每日 Token 配额：0 → 50000」。"""
    text = str(raw or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
    if not isinstance(data, dict):
        return text
    field = data.get("字段")
    if field is not None and ("旧值" in data or "新值" in data):
        return "%s：%s → %s" % (
            DETAIL_FIELD_LABELS.get(str(field), str(field)),
            data.get("旧值", "—"),
            data.get("新值", "—"),
        )
    return "；".join(f"{k}={v}" for k, v in data.items()) or text


class AdminPage(QWidget):
    """管理后台容器：内部 8 个 Tab。"""

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "管理后台",
            "平台级管理：用户与权限、用量与成本、内容治理、系统健康与审计。仅管理员可见。",
        ))

        self.tabs = QTabWidget()
        self.tabs.addTab(OverviewTab(self), "管理总览")
        self.tabs.addTab(UsersTab(self), "用户管理")
        self.tabs.addTab(UsageTab(self), "用量与成本")
        self.tabs.addTab(ContentTab(self), "内容治理")
        self.tabs.addTab(HealthTab(self), "系统健康")
        self.tabs.addTab(AuditTab(self), "审计日志")
        self.tabs.addTab(SettingsTab(self), "系统设置")
        self.tabs.addTab(SecurityTab(self), "账号安全")
        layout.addWidget(self.tabs, 1)

        # 懒加载：进页面 / 切 Tab 时才拉数据（各 Tab 在自己的 load() 里发请求）
        self.tabs.currentChanged.connect(self._reload_current)

    def _reload_current(self) -> None:
        widget = self.tabs.currentWidget()
        load = getattr(widget, "load", None)
        if callable(load):
            load()

    def showEvent(self, event):
        """每次进入管理后台刷新当前 Tab（后台数据随时在变）。"""
        super().showEvent(event)
        self._reload_current()


# ==================== 1. 管理总览 ====================

class OverviewTab(QWidget):
    def __init__(self, _parent):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(QLabel("统计窗口"))
        self.days_combo = QComboBox()
        for label, days in (("近 7 天", 7), ("近 30 天", 30), ("近 90 天", 90)):
            self.days_combo.addItem(label, days)
        self.days_combo.setCurrentIndex(1)
        self.days_combo.currentIndexChanged.connect(self.load)
        head.addWidget(self.days_combo)
        head.addStretch(1)
        btn = QPushButton("刷新")
        btn.clicked.connect(self.load)
        head.addWidget(btn)
        layout.addLayout(head)

        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(12)
        layout.addLayout(self.cards_row)

        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        layout.addLayout(self.body)
        layout.addStretch(1)

    def load(self, *_args):
        days = self.days_combo.currentData() or 30
        _async(self, lambda: api.admin_overview(int(days)), self._render)

    def _render(self, ov: Dict[str, Any]) -> None:
        _clear(self.cards_row)
        _clear(self.body)
        banner = _err_banner(ov)
        if banner:
            self.body.addWidget(banner)
            return

        users = ov.get("users") or {}
        usage = ov.get("usage") or {}
        content = ov.get("content") or {}
        self.cards_row.addWidget(stat_card(
            "用户总数", str(users.get("total") or 0), accent="#4f46e5",
            sub=f"近 7 天新增 {users.get('new_7d') or 0} · 活跃 {users.get('active_7d') or 0}"))
        self.cards_row.addWidget(stat_card(
            "管理员", str(users.get("admins") or 0), accent="#8b5cf6",
            sub=f"自带 Key {users.get('byok') or 0} · 贡献语料 {users.get('contributing') or 0}"))
        self.cards_row.addWidget(stat_card(
            "AI 调用", str(usage.get("calls") or 0), accent="#06b6d4",
            sub=f"失败 {usage.get('failed') or 0} · 失败率 {usage.get('fail_rate') or 0:.0%}"))
        self.cards_row.addWidget(stat_card(
            "成本", _fmt_cost(usage.get("cost_cny")), accent="#14b8a6",
            sub=f"{int(usage.get('tokens') or 0):,} token · 均 {int(usage.get('avg_duration_ms') or 0)}ms"))

        # 内容规模：4 列等宽网格（早先用 QHBoxLayout 顺序排，
        # 各格宽度随数字/标签长短变化，数字和标签会对不齐）
        c_card = card()
        cl = QVBoxLayout(c_card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(12)
        cl.addWidget(section_label("内容规模"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(10)
        items = (("简历", "resumes"), ("岗位", "jds"), ("投递", "applications"),
                 ("模拟面试", "interview_sessions"), ("共享语料", "corpus"),
                 ("帖子", "posts"), ("评论", "comments"), ("建议反馈", "feedbacks"))
        for i, (label, key) in enumerate(items):
            box = QVBoxLayout()
            box.setSpacing(4)
            num = QLabel(str(content.get(key) or 0))
            num.setStyleSheet("font-size: 20px; font-weight: 800;")
            box.addWidget(num)
            box.addWidget(hint_label(label))
            grid.addLayout(box, i // 4, i % 4)
        for col in range(4):
            grid.setColumnStretch(col, 1)
        cl.addLayout(grid)
        self.body.addWidget(c_card)

        # 趋势：横向条形（不引图表依赖）
        trend = ov.get("trend") or []
        if trend:
            t_card = card()
            tl = QVBoxLayout(t_card)
            tl.setContentsMargins(16, 14, 16, 14)
            tl.setSpacing(8)
            tl.addWidget(section_label("每日调用趋势"))
            window = trend[-14:]
            peak = max((int(t.get("calls") or 0) for t in window), default=0)
            if not peak:
                tl.addWidget(hint_label("最近两周没有模型调用记录。"))
            else:
                # 用 f-string：中文逗号紧跟 % 会被当成格式符（%，→ ValueError）
                tl.addWidget(hint_label(f"最多的一天为 100%，共 {peak} 次。"))
            for t in window:
                calls = int(t.get("calls") or 0)
                pct = int(round(calls / peak * 100)) if peak else 0
                row = QHBoxLayout()
                row.setSpacing(12)
                day = hint_label(str(t.get("date") or "")[5:])
                day.setFixedWidth(44)
                row.addWidget(day)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(pct)
                bar.setTextVisible(False)
                bar.setFixedHeight(8)
                bar.setStyleSheet(
                    "QProgressBar { background: rgba(148,163,184,0.18); border: none;"
                    " border-radius: 4px; }"
                    "QProgressBar::chunk { background: #6366f1; border-radius: 4px; }")
                row.addWidget(bar, 1)
                meta = hint_label(f"{calls} 次 · {_fmt_cost(t.get('cost_cny'))}")
                meta.setWordWrap(False)
                meta.setFixedWidth(140)
                meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                row.addWidget(meta)
                tl.addLayout(row)
            self.body.addWidget(t_card)

        for it in (ov.get("alerts") or [])[:5]:
            text = it if isinstance(it, str) else (it.get("message") or str(it))
            warn = QLabel(f"⚠ {text}")
            warn.setWordWrap(True)
            warn.setProperty("class", "text-warn")
            self.body.addWidget(warn)


# ==================== 2. 用户管理 ====================

class UsersTab(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.page = parent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        self._users: List[Dict[str, Any]] = []

        head = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按邮箱 / 昵称 / ID 搜索…")
        self.search_edit.returnPressed.connect(lambda: self.load(page=1))
        head.addWidget(self.search_edit, 1)
        self.role_combo = QComboBox()
        self.role_combo.addItem("全部角色", "")
        self.role_combo.addItem("管理员", "admin")
        self.role_combo.addItem("普通用户", "user")
        self.role_combo.currentIndexChanged.connect(lambda: self.load(page=1))
        head.addWidget(self.role_combo)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(lambda: self.load(page=1))
        head.addWidget(refresh_btn)
        export_btn = QPushButton("导出 CSV")
        export_btn.clicked.connect(self._export)
        head.addWidget(export_btn)
        layout.addLayout(head)

        self.banner_host = QVBoxLayout()
        layout.addLayout(self.banner_host)

        self.table = _table(["昵称", "邮箱", "角色", "状态", "调用", "花费", "注册时间"], stretch_col=1)
        layout.addWidget(self.table, 1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.role_btn = QPushButton("切换角色")
        self.role_btn.clicked.connect(self._toggle_role)
        self.active_btn = QPushButton("启用 / 停用")
        self.active_btn.clicked.connect(self._toggle_active)
        self.pwd_btn = QPushButton("重置密码")
        self.pwd_btn.clicked.connect(self._reset_password)
        self.email_btn = QPushButton("改绑邮箱")
        self.email_btn.clicked.connect(self._bind_email)
        self.detail_btn = QPushButton("查看详情")
        self.detail_btn.clicked.connect(self._show_detail)
        for b in (self.role_btn, self.active_btn, self.pwd_btn, self.email_btn, self.detail_btn):
            self.actions.addWidget(b)
        self.actions.addStretch(1)
        self.pager = QHBoxLayout()
        prev_btn = QPushButton("上一页")
        prev_btn.clicked.connect(lambda: self.load(self._page - 1))
        self.page_label = hint_label("")
        next_btn = QPushButton("下一页")
        next_btn.clicked.connect(lambda: self.load(self._page + 1))
        self.pager.addStretch(1)
        self.pager.addWidget(prev_btn)
        self.pager.addWidget(self.page_label)
        self.pager.addWidget(next_btn)
        layout.addLayout(self.actions)
        layout.addLayout(self.pager)

        self._page = 1
        self._total = 0

    def _selected(self) -> Optional[Dict[str, Any]]:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._users):
            QMessageBox.information(self, "未选择", "请先在列表里选中一个用户")
            return None
        return self._users[row]

    def load(self, page: int = 1):
        self._page = max(1, int(page or 1))
        role = self.role_combo.currentData() or ""
        q = self.search_edit.text().strip()
        _async(self, lambda: api.admin_list_users(
            q=q or None, role=role or None, page=self._page, page_size=20),
            self._render)

    def _render(self, data: Dict[str, Any]) -> None:
        _clear(self.banner_host)
        banner = _err_banner(data)
        if banner:
            self.banner_host.addWidget(banner)
            return
        self._users = data.get("items") or []
        self._total = int(data.get("total") or 0)
        self.table.setRowCount(len(self._users))
        for i, u in enumerate(self._users):
            usage = u.get("usage") or {}
            cells = [
                str(u.get("name") or "（未设昵称）"),
                str(u.get("email") or ""),
                api.ROLE_LABELS.get(str(u.get("role")), str(u.get("role"))),
                "启用" if u.get("is_active") else "已停用",
                str(int(usage.get("calls") or 0)),
                _fmt_cost(usage.get("cost_cny")),
                _fmt_time(u.get("created_at")),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 3 and not u.get("is_active"):
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(i, col, item)
        pages = max(1, (self._total + 19) // 20)
        self.page_label.setText(f"第 {self._page} / {pages} 页 · 共 {self._total} 人")

    def refresh(self):
        self.load(self._page)

    def _toggle_role(self):
        u = self._selected()
        if not u:
            return
        new_role = "user" if u.get("role") == "admin" else "admin"
        tip = ("要把该用户**提升为管理员**吗？" if new_role == "admin"
               else "要把该用户**降为普通用户**吗？")
        if QMessageBox.question(self, "切换角色", f"{tip}\n\n{u.get('email')}") \
                != QMessageBox.StandardButton.Yes:
            return
        _async(self, lambda: api.admin_update_user_role(u["id"], new_role),
               lambda _r: self.refresh(), self._fail)

    def _toggle_active(self):
        u = self._selected()
        if not u:
            return
        target = not bool(u.get("is_active"))
        verb = "启用" if target else "停用"
        if QMessageBox.question(self, f"{verb}账号",
                                f"确定要{verb} {u.get('email')} 吗？") \
                != QMessageBox.StandardButton.Yes:
            return
        _async(self, lambda: api.admin_update_user_active(u["id"], target),
               lambda _r: self.refresh(), self._fail)

    def _reset_password(self):
        u = self._selected()
        if not u:
            return
        from PySide6.QtWidgets import QInputDialog
        pwd, ok = QInputDialog.getText(
            self, "重置密码", f"为 {u.get('email')} 设置新密码（至少 6 位）：",
            QLineEdit.EchoMode.Password)
        if not ok or not pwd:
            return
        if len(pwd) < 6:
            QMessageBox.warning(self, "密码过短", "新密码至少 6 位")
            return
        _async(self, lambda: api.admin_reset_user_password(u["id"], pwd),
               lambda msg: QMessageBox.information(self, "已重置", str(msg)),
               self._fail)

    def _bind_email(self):
        u = self._selected()
        if not u:
            return
        from PySide6.QtWidgets import QInputDialog
        email, ok = QInputDialog.getText(
            self, "改绑邮箱", f"为 {u.get('name')} 设置新登录邮箱：", text=str(u.get("email") or ""))
        if not ok or not email:
            return
        _async(self, lambda: api.admin_bind_user_email(u["id"], email),
               lambda msg: QMessageBox.information(self, "已改绑", str(msg)),
               self._fail)

    def _show_detail(self):
        u = self._selected()
        if not u:
            return
        _async(self, lambda: api.admin_user_detail(u["id"]), lambda d: self._render_detail(u, d))

    def _render_detail(self, u: Dict[str, Any], detail: Dict[str, Any]) -> None:
        if detail.get("_error"):
            QMessageBox.warning(self, "读取失败", str(detail.get("_error")))
            return
        content = detail.get("content") or {}
        usage = detail.get("usage") or {}
        lines = [
            f"用户：{u.get('name')} <{u.get('email')}>",
            f"角色：{api.ROLE_LABELS.get(str(u.get('role')), u.get('role'))}"
            f"　状态：{'启用' if u.get('is_active') else '已停用'}",
            f"注册时间：{_fmt_time(u.get('created_at'))}",
            "",
            f"AI 调用：{int(usage.get('calls') or 0)} 次 · "
            f"{int(usage.get('tokens') or 0):,} token · {_fmt_cost(usage.get('cost_cny'))}",
            "",
            "内容：",
        ]
        for label, key in (("简历", "resumes"), ("岗位", "jds"), ("投递", "applications"),
                           ("模拟面试", "interview_sessions"), ("面经", "articles")):
            if key in content:
                lines.append(f"　{label}：{content.get(key)}")
        QMessageBox.information(self, "用户详情", "\n".join(lines))

    def _export(self):
        from PySide6.QtWidgets import QFileDialog
        from ... import database as db
        path, _ = QFileDialog.getSaveFileName(
            self, "导出用户列表", db.resolve_export_dir() + "/users.csv", "CSV (*.csv)")
        if not path:
            return
        _async(self, lambda: api.admin_export_users(path),
               lambda p: QMessageBox.information(self, "已导出", f"用户列表已导出到：\n{p}"),
               self._fail)

    def _fail(self, msg: str):
        QMessageBox.warning(self, "操作失败", str(msg))


# ==================== 3. 用量与成本 ====================

DIMENSIONS = [("按功能", "operation"), ("按用户", "user"),
              ("按模型", "model"), ("按日期", "day")]


class UsageTab(QWidget):
    def __init__(self, _parent):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(QLabel("统计维度"))
        self.dim_combo = QComboBox()
        for label, key in DIMENSIONS:
            self.dim_combo.addItem(label, key)
        self.dim_combo.currentIndexChanged.connect(self.load)
        head.addWidget(self.dim_combo)
        head.addStretch(1)
        btn = QPushButton("刷新")
        btn.clicked.connect(self.load)
        head.addWidget(btn)
        layout.addLayout(head)

        self.banner_host = QVBoxLayout()
        layout.addLayout(self.banner_host)
        self.summary_row = QHBoxLayout()
        layout.addLayout(self.summary_row)
        self.table = _table(["维度", "调用", "Token", "花费", "均耗时", "失败"], stretch_col=0)
        layout.addWidget(self.table, 1)

    def load(self, *_args):
        dim = self.dim_combo.currentData() or "operation"
        _async(self, lambda: api.admin_usage(dim, days=30, limit=50), self._render)

    def _render(self, data: Dict[str, Any]) -> None:
        _clear(self.banner_host)
        _clear(self.summary_row)
        banner = _err_banner(data)
        if banner:
            self.banner_host.addWidget(banner)
            return
        total = data.get("total") or {}
        self.summary_row.addWidget(stat_card(
            "总调用", str(int(total.get("calls") or 0)), accent="#4f46e5", sub="近 30 天"))
        self.summary_row.addWidget(stat_card(
            "总 Token", f"{int(total.get('tokens') or 0):,}", accent="#8b5cf6", sub="输入 + 输出"))
        self.summary_row.addWidget(stat_card(
            "总花费", _fmt_cost(total.get("cost_cny")), accent="#06b6d4", sub="按参考汇率折算"))

        rows = data.get("items") or []
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            key = str(r.get("key") or "-")
            if (data.get("dimension") or "") == "operation":
                key = OPERATION_LABELS.get(key, key)
            cells = [
                key,
                str(int(r.get("calls") or 0)),
                f"{int(r.get('tokens') or 0):,}",
                _fmt_cost(r.get("cost_cny")),
                f"{int(r.get('avg_duration_ms') or 0)}ms",
                str(int(r.get("failed") or 0)),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 5 and int(r.get("failed") or 0) > 0:
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(i, col, item)


# ==================== 4. 内容治理 ====================

FEEDBACK_STATUS = [("待处理", "pending"), ("已采纳", "accepted"),
                   ("已婉拒", "declined"), ("已回复", "replied")]


class ContentTab(QWidget):
    def __init__(self, _parent):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        sub = QTabWidget()
        sub.addTab(self._community_tab(), "社区内容")
        sub.addTab(self._feedback_tab(), "建议反馈")
        layout.addWidget(sub)

    # ---------- 社区内容 ----------
    def _community_tab(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(hint_label("管理分享墙的帖子与评论。删除不可恢复。"), 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._load_posts)
        head.addWidget(refresh)
        self.del_post_btn = QPushButton("删除帖子")
        self.del_post_btn.setProperty("class", "danger")
        self.del_post_btn.clicked.connect(self._delete_post)
        head.addWidget(self.del_post_btn)
        self.comments_btn = QPushButton("查看 / 删评论")
        self.comments_btn.clicked.connect(self._open_comments)
        head.addWidget(self.comments_btn)
        layout.addLayout(head)

        self.posts_banner = QVBoxLayout()
        layout.addLayout(self.posts_banner)
        self.posts_table = _table(
            ["标题", "作者", "分类", "点赞", "评论", "发布时间"], stretch_col=0)
        self._posts: List[Dict[str, Any]] = []
        layout.addWidget(self.posts_table, 1)
        return host

    def load(self, *_args):
        """统一加载入口（由 AdminPage 在切到本 Tab 时调用）。

        ⚠️ 不要在 __init__ 里直接发请求：构造期起后台线程，页面若在请求返回前
        被销毁，回调会摸到已删除的 C++ 对象，直接令进程崩溃（已实测）。
        """
        self._load_posts()
        self._load_feedbacks()

    def _load_posts(self):
        _async(self, lambda: api.list_posts(page=1, page_size=100), self._render_posts)

    def _render_posts(self, data: Dict[str, Any]) -> None:
        _clear(self.posts_banner)
        banner = _err_banner(data)
        if banner:
            self.posts_banner.addWidget(banner)
            return
        self._posts = data.get("items") or []
        if not self._posts:
            self.posts_banner.addWidget(_empty_hint(
                "还没有帖子。用户在「交流中心」分享后，会出现在这里。"))
        self.posts_table.setRowCount(len(self._posts))
        for i, p in enumerate(self._posts):
            cells = [
                str(p.get("title") or "（无标题）"),
                str(p.get("author") or "匿名"),
                str(p.get("category") or ""),
                str(int(p.get("like_count") or 0)),
                str(int(p.get("comment_count") or 0)),
                _fmt_time(p.get("created_at")),
            ]
            for col, text in enumerate(cells):
                self.posts_table.setItem(i, col, QTableWidgetItem(text))

    def _selected_post(self) -> Optional[Dict[str, Any]]:
        row = self.posts_table.currentRow()
        if row < 0 or row >= len(self._posts):
            QMessageBox.information(self, "未选择", "请先在列表里选中一个帖子")
            return None
        return self._posts[row]

    def _delete_post(self):
        p = self._selected_post()
        if not p:
            return
        if QMessageBox.question(
                self, "删除帖子",
                f"确定删除帖子「{p.get('title')}」？\n\n该操作不可恢复。") \
                != QMessageBox.StandardButton.Yes:
            return
        _async(self, lambda: api.admin_delete_post(p["id"]), lambda _r: self._load_posts(),
               lambda m: QMessageBox.warning(self, "删除失败", str(m)))

    def _open_comments(self):
        p = self._selected_post()
        if not p:
            return
        _async(self, lambda: api.get_post_detail(p["id"]),
               lambda d: self._show_comments(p, d))

    def _show_comments(self, post: Dict[str, Any], detail: Dict[str, Any]) -> None:
        comments = (detail or {}).get("comments") or []
        dlg = QWidget(self, Qt.WindowType.Window)
        dlg.setWindowTitle(f"评论 · {post.get('title')}")
        dlg.resize(560, 460)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(section_label(f"共 {len(comments)} 条评论"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 6, 0)
        box.setSpacing(8)
        if not comments:
            box.addWidget(hint_label("这个帖子还没有评论。"))
        for c in comments:
            row = card()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            info = QVBoxLayout()
            info.setSpacing(4)
            who = QLabel(str(c.get("author") or "匿名"))
            who.setStyleSheet("font-size: 12px; font-weight: 700;")
            text = hint_label(f"{c.get('content') or ''}　·　{_fmt_time(c.get('created_at'))}")
            text.setWordWrap(True)
            info.addWidget(who)
            info.addWidget(text)
            rl.addLayout(info, 1)
            del_btn = QPushButton("删除")
            del_btn.setProperty("class", "danger")
            del_btn.clicked.connect(
                lambda _checked=False, cid=c.get("id"): self._delete_comment(cid, dlg))
            rl.addWidget(del_btn)
            box.addWidget(row)
        box.addStretch(1)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        close = QPushButton("关闭")
        close.clicked.connect(dlg.close)
        layout.addWidget(close)
        dlg.show()
        self._comments_dlg = dlg

    def _delete_comment(self, comment_id, dlg):
        if QMessageBox.question(self, "删除评论", "确定删除这条评论？该操作不可恢复。") \
                != QMessageBox.StandardButton.Yes:
            return
        _async(self, lambda: api.delete_post_comment(comment_id),
               lambda _r: (dlg.close(), self._load_posts(), self._open_comments()),
               lambda m: QMessageBox.warning(self, "删除失败", str(m)))

    # ---------- 建议反馈 ----------
    def _feedback_tab(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(hint_label("处理用户建议：改状态、写官方回复（会公开显示）。"), 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._load_feedbacks)
        head.addWidget(refresh)
        edit_btn = QPushButton("处理选中反馈")
        edit_btn.setProperty("class", "primary")
        edit_btn.clicked.connect(self._edit_feedback)
        head.addWidget(edit_btn)
        layout.addLayout(head)

        self.fb_banner = QVBoxLayout()
        self.fb_banner.setSpacing(8)
        layout.addLayout(self.fb_banner)
        self.fb_table = _table(
            ["标题", "分类", "状态", "提交人", "点赞", "时间"], stretch_col=0)
        self._feedbacks: List[Dict[str, Any]] = []
        layout.addWidget(self.fb_table, 1)
        return host

    def _load_feedbacks(self):
        _async(self, lambda: api.list_feedbacks(page=1, page_size=100), self._render_feedbacks)

    def _render_feedbacks(self, data: Dict[str, Any]) -> None:
        _clear(self.fb_banner)
        banner = _err_banner(data)
        if banner:
            self.fb_banner.addWidget(banner)
            self.fb_table.setRowCount(0)
            return
        self._feedbacks = data.get("items") or []
        if not self._feedbacks:
            self.fb_banner.addWidget(_empty_hint(
                "还没有用户提交建议。用户在「交流中心 → 建议反馈」提交后会出现在这里。"))
        labels = dict((v, k) for k, v in api.FEEDBACK_STATUS_LABELS.items())
        self.fb_table.setRowCount(len(self._feedbacks))
        for i, f in enumerate(self._feedbacks):
            status = str(f.get("status") or "pending")
            cells = [
                str(f.get("title") or "（无标题）"),
                str(f.get("category") or ""),
                labels.get(status, status),
                str(f.get("author") or "匿名"),
                str(int(f.get("like_count") or 0)),
                _fmt_time(f.get("created_at")),
            ]
            for col, text in enumerate(cells):
                self.fb_table.setItem(i, col, QTableWidgetItem(text))

    def _edit_feedback(self):
        row = self.fb_table.currentRow()
        if row < 0 or row >= len(self._feedbacks):
            QMessageBox.information(self, "未选择", "请先在列表里选中一条反馈")
            return
        fb = self._feedbacks[row]
        dlg = QWidget(self, Qt.WindowType.Window)
        dlg.setWindowTitle(f"处理反馈 · {fb.get('title')}")
        dlg.resize(520, 420)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("用户提交"))

        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(str(fb.get("content") or "（无内容）"))
        body.setMaximumHeight(120)
        layout.addWidget(body)

        layout.addWidget(section_label("处理"))
        row_box = QHBoxLayout()
        row_box.addWidget(QLabel("状态"))
        status_combo = QComboBox()
        current = str(fb.get("status") or "pending")
        for label, key in FEEDBACK_STATUS:
            status_combo.addItem(label, key)
        idx = status_combo.findData(current)
        if idx >= 0:
            status_combo.setCurrentIndex(idx)
        row_box.addWidget(status_combo)
        row_box.addStretch(1)
        layout.addLayout(row_box)

        reply = QTextEdit()
        reply.setPlaceholderText("官方回复（会公开显示在该建议下方，可留空）")
        reply.setPlainText(str(fb.get("admin_reply") or ""))
        layout.addWidget(reply, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.close)
        save = QPushButton("保存")
        save.setProperty("class", "primary")

        def do_save():
            status = status_combo.currentData()
            text = reply.toPlainText().strip()
            _async(self,
                   lambda: api.admin_update_feedback(fb["id"], status, text),
                   lambda _r: (dlg.close(), self._load_feedbacks(),
                               QMessageBox.information(self, "已保存", "反馈处理结果已更新")),
                   lambda m: QMessageBox.warning(self, "保存失败", str(m)))

        save.clicked.connect(do_save)
        btns.addWidget(cancel)
        btns.addWidget(save)
        layout.addLayout(btns)
        dlg.show()
        self._fb_dlg = dlg


# ==================== 5. 系统健康 ====================

class HealthTab(QWidget):
    def __init__(self, _parent):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(hint_label("服务状态、默认模型 Key 与近期异常调用。"), 1)
        btn = QPushButton("重新检测")
        btn.clicked.connect(self.load)
        head.addWidget(btn)
        outer.addLayout(head)

        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        outer.addLayout(self.body)
        outer.addStretch(1)

    def load(self, *_args):
        _async(self, api.admin_health, self._render)

    def _render(self, h: Dict[str, Any]) -> None:
        _clear(self.body)
        banner = _err_banner(h)
        if banner:
            self.body.addWidget(banner)
            return
        ok = str(h.get("status") or "") == "ok"
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(stat_card("服务状态", "正常" if ok else "异常",
                                accent="#059669" if ok else "#dc2626",
                                sub=f"检测于 {_fmt_time(h.get('checked_at'))}"))
        db_ok = bool((h.get("database") or {}).get("ok"))
        row.addWidget(stat_card("数据库", "正常" if db_ok else "异常",
                                accent="#059669" if db_ok else "#dc2626"))
        key = h.get("default_key") or {}
        row.addWidget(stat_card("平台默认 Key", "已配置" if key.get("configured") else "未配置",
                                accent="#4f46e5", sub=str(key.get("model") or "")))
        traces = h.get("traces") or {}
        row.addWidget(stat_card("调用记录", str(int(traces.get("total") or 0)),
                                accent="#8b5cf6",
                                sub=f"近 1 小时 {traces.get('last_1h') or 0} · "
                                    f"近 24 小时 {traces.get('last_24h') or 0}"))
        self.body.addLayout(row)

        failures = h.get("recent_failures") or []
        f_card = card(success=not failures, danger=bool(failures))
        fl = QVBoxLayout(f_card)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.setSpacing(8)
        fl.addWidget(section_label("近期失败调用"))
        if not failures:
            fl.addWidget(hint_label("没有失败记录。"))
        else:
            for f in failures[:8]:
                line = (f if isinstance(f, str) else
                        f"{_fmt_time(f.get('created_at'))} · {f.get('operation')} · "
                        f"{f.get('error') or f.get('status') or ''}")
                item = hint_label("· " + str(line))
                item.setWordWrap(True)
                fl.addWidget(item)
        self.body.addWidget(f_card)

        slowest = h.get("slowest") or []
        if slowest:
            s_card = card()
            sl = QVBoxLayout(s_card)
            sl.setContentsMargins(16, 14, 16, 14)
            sl.setSpacing(8)
            sl.addWidget(section_label("最慢的调用"))
            table = _table(["时间", "用户", "功能", "状态", "Token", "耗时"], stretch_col=1)
            table.setRowCount(len(slowest))
            for i, s in enumerate(slowest):
                cells = [
                    _fmt_time(s.get("created_at")),
                    str(s.get("user_name") or s.get("user_id") or "-"),
                    OPERATION_LABELS.get(str(s.get("operation")), str(s.get("operation") or "-")),
                    "成功" if s.get("status") == "success" else "失败",
                    f"{int(s.get('tokens') or 0):,}",
                    fmt_duration(int(s.get("duration_ms") or 0)),
                ]
                for col, text in enumerate(cells):
                    table.setItem(i, col, QTableWidgetItem(text))
            table.setMinimumHeight(160)
            sl.addWidget(table)
            self.body.addWidget(s_card)


# ==================== 6. 审计日志 ====================

class AuditTab(QWidget):
    def __init__(self, _parent):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(QLabel("动作"))
        self.action_combo = QComboBox()
        self.action_combo.addItem("全部动作", "")
        self.action_combo.currentIndexChanged.connect(lambda: self.load(1))
        head.addWidget(self.action_combo)
        head.addStretch(1)
        btn = QPushButton("刷新")
        btn.clicked.connect(lambda: self.load(self._page))
        head.addWidget(btn)
        layout.addLayout(head)

        self.banner_host = QVBoxLayout()
        layout.addLayout(self.banner_host)
        self.table = _table(["时间", "操作人", "动作", "对象", "详情", "IP"], stretch_col=4)
        layout.addWidget(self.table, 1)

        pager = QHBoxLayout()
        prev_btn = QPushButton("上一页")
        prev_btn.clicked.connect(lambda: self.load(self._page - 1))
        self.page_label = hint_label("")
        next_btn = QPushButton("下一页")
        next_btn.clicked.connect(lambda: self.load(self._page + 1))
        pager.addStretch(1)
        pager.addWidget(prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(next_btn)
        layout.addLayout(pager)

        self._page = 1
        self._total = 0

    def load(self, page: int = 1):
        self._page = max(1, int(page or 1))
        action = self.action_combo.currentData() or ""
        _async(self, lambda: api.admin_audit_logs(
            page=self._page, page_size=20, action=action or None), self._render)

    def _render(self, data: Dict[str, Any]) -> None:
        _clear(self.banner_host)
        banner = _err_banner(data)
        if banner:
            self.banner_host.addWidget(banner)
            return
        actions = data.get("actions") or []
        if actions and self.action_combo.count() <= 1:
            for a in actions:
                self.action_combo.addItem(ACTION_LABELS.get(a, a), a)

        items = data.get("items") or []
        self._total = int(data.get("total") or 0)
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            target = str(it.get("target_label") or it.get("target_id") or "-")
            cells = [
                _fmt_time(it.get("created_at")),
                str(it.get("operator_name") or it.get("operator_id") or "-"),
                ACTION_LABELS.get(str(it.get("action")), str(it.get("action") or "-")),
                DETAIL_FIELD_LABELS.get(target, target),
                _fmt_detail(it.get("detail")),
                str(it.get("ip") or "-"),
            ]
            for col, text in enumerate(cells):
                self.table.setItem(i, col, QTableWidgetItem(text))
        pages = max(1, (self._total + 19) // 20)
        self.page_label.setText(f"第 {self._page} / {pages} 页 · 共 {self._total} 条")


# ==================== 7. 系统设置 ====================

ACTION_LABELS = {
    "update_role": "改角色",
    "update_active": "启停账号",
    "update_setting": "改平台设置",
    "reset_password": "重置密码",
    "bind_email": "改绑邮箱",
    "delete_post": "删除帖子",
    "delete_comment": "删除评论",
    "update_feedback": "处理反馈",
}

SETTING_LABELS = {
    "quota_daily_tokens": "每日 Token 配额",
    "allow_register": "开放新用户注册",
    "announcement": "平台公告内容",
    "announcement_enabled": "展示平台公告",
}

# 审计日志里「对象」列与「详情」里的字段名 → 中文（后端存的是英文字段）
DETAIL_FIELD_LABELS = {
    **SETTING_LABELS,
    "role": "角色",
    "is_active": "账号状态",
    "name": "昵称",
    "email": "邮箱",
    "status": "状态",
}


class SettingsTab(QWidget):
    def __init__(self, _parent):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(hint_label("平台级开关与公告。改动会写入审计日志。"), 1)
        btn = QPushButton("刷新")
        btn.clicked.connect(self.load)
        head.addWidget(btn)
        outer.addLayout(head)

        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        outer.addLayout(self.body)
        outer.addStretch(1)
        self._editors: Dict[str, Any] = {}

    def load(self, *_args):
        _async(self, api.admin_settings, self._render)

    def _render(self, data: Dict[str, Any]) -> None:
        _clear(self.body)
        self._editors = {}
        banner = _err_banner(data)
        if banner:
            self.body.addWidget(banner)
            return

        pm = data.get("platform_model") or {}
        info = card()
        il = QVBoxLayout(info)
        il.setContentsMargins(16, 14, 16, 14)
        il.setSpacing(4)
        il.addWidget(section_label("平台默认模型"))
        il.addWidget(hint_label(
            f"模型：{pm.get('model') or '—'}　"
            f"Key：{'已配置' if pm.get('key_configured') else '未配置'}　"
            f"地址：{pm.get('base_url') or '—'}"))
        il.addWidget(hint_label("未配置自有 Key 的用户会使用这个模型。"))
        self.body.addWidget(info)

        settings = data.get("settings") or {}
        for key, item in settings.items():
            label = SETTING_LABELS.get(key, key)
            box = card()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(16, 14, 16, 14)
            bl.setSpacing(8)
            bl.addWidget(section_label(label))
            bl.addWidget(hint_label(str(item.get("description") or "")))
            value = item.get("value")

            row = QHBoxLayout()
            row.setSpacing(12)
            if isinstance(value, bool):
                editor = QCheckBox("开启")
                editor.setChecked(value)
                # 短控件 + stretch：保存按钮固定在右侧，不被输入框挤到天边
                row.addWidget(editor)
                row.addStretch(1)
            elif isinstance(value, int):
                editor = QLineEdit(str(value))
                editor.setMaximumWidth(200)
                row.addWidget(editor)
                row.addStretch(1)
            else:
                editor = QLineEdit(str(value or ""))
                editor.setPlaceholderText("留空则不显示")
                row.addWidget(editor, 1)
            save = QPushButton("保存")
            save.setProperty("class", "primary")
            save.clicked.connect(
                lambda _checked=False, k=key, e=editor, orig=value: self._save(k, e, orig))
            row.addWidget(save)
            bl.addLayout(row)
            self._editors[key] = editor
            self.body.addWidget(box)

    def _save(self, key: str, editor, original):
        if isinstance(editor, QCheckBox):
            value = bool(editor.isChecked())
        elif isinstance(original, int):
            try:
                value = int(str(editor.text()).strip() or 0)
            except ValueError:
                QMessageBox.warning(self, "格式错误", "这一项要填整数")
                return
        else:
            value = editor.text().strip()
        _async(self, lambda: api.admin_update_setting(key, value),
               lambda _r: QMessageBox.information(self, "已保存", f"{SETTING_LABELS.get(key, key)} 已更新"),
               lambda m: QMessageBox.warning(self, "保存失败", str(m)))


# ==================== 8. 账号安全 ====================

class SecurityTab(QWidget):
    """账号安全：当前账号 / 登录密码 / 登录邮箱 / 登录会话。

    早先只有一张「修改密码」卡，页面下部一大片空白；这里补齐账号信息、
    换绑邮箱与退出登录，页面有内容、也和系统设置里的账号区保持一致。
    """

    def __init__(self, parent):
        super().__init__()
        self.page = parent
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(12)

        # 1) 当前账号
        acc = card()
        al = QVBoxLayout(acc)
        al.setContentsMargins(16, 14, 16, 14)
        al.setSpacing(4)
        al.addWidget(section_label("当前登录账号"))
        self.name_label = QLabel("—")
        self.name_label.setStyleSheet("font-size: 15px; font-weight: 700;")
        al.addWidget(self.name_label)
        self.meta_label = hint_label("")
        al.addWidget(self.meta_label)
        outer.addWidget(acc)

        # 2) 登录密码
        pwd_card = card()
        pl = QVBoxLayout(pwd_card)
        pl.setContentsMargins(16, 14, 16, 14)
        pl.setSpacing(8)
        pl.addWidget(section_label("登录密码"))
        pl.addWidget(hint_label(
            "校验当前密码后设置新密码（至少 6 位）。修改后当前登录仍有效，"
            "下次登录请使用新密码。"))
        pwd_row = QHBoxLayout()
        pwd_btn = QPushButton("修改密码")
        pwd_btn.setProperty("class", "primary")
        pwd_btn.clicked.connect(self._open_change_password)
        pwd_row.addWidget(pwd_btn)
        pwd_row.addStretch(1)
        pl.addLayout(pwd_row)
        outer.addWidget(pwd_card)

        # 3) 登录邮箱
        mail_card = card()
        ml = QVBoxLayout(mail_card)
        ml.setContentsMargins(16, 14, 16, 14)
        ml.setSpacing(8)
        ml.addWidget(section_label("登录邮箱"))
        ml.addWidget(hint_label(
            "邮箱同时是登录账号。换绑后本机「记住账号」会同步更新，"
            "下次登录请使用新邮箱。"))
        mail_row = QHBoxLayout()
        mail_btn = QPushButton("换绑邮箱")
        mail_btn.clicked.connect(self._open_change_email)
        mail_row.addWidget(mail_btn)
        mail_row.addStretch(1)
        ml.addLayout(mail_row)
        outer.addWidget(mail_card)

        # 4) 登录会话
        sess = card()
        sl = QVBoxLayout(sess)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        sl.addWidget(section_label("登录会话"))
        sl.addWidget(hint_label(
            "登录凭证保存在本机，退出登录会清除本机凭证并返回登录界面。"))
        out_row = QHBoxLayout()
        logout_btn = QPushButton("退出登录")
        logout_btn.setProperty("class", "danger")
        logout_btn.clicked.connect(self._confirm_logout)
        out_row.addWidget(logout_btn)
        out_row.addStretch(1)
        sl.addLayout(out_row)
        outer.addWidget(sess)

        outer.addStretch(1)

    def load(self, *_args):
        """只读本机缓存的登录态（不发请求）。"""
        u = api.get_user() or {}
        name = str(u.get("name") or "").strip()
        email = str(u.get("email") or "").strip() or (api.get_active_user() or "")
        role = str(u.get("role") or "user").lower()
        self.name_label.setText(name or email or "未登录")
        self.meta_label.setText(f"{email}　·　{api.ROLE_LABELS.get(role, role)}")

    def _open_change_password(self):
        dlg = ChangePasswordDialog(self)
        dlg.exec()
        if dlg.changed:
            QMessageBox.information(
                self, "密码已修改",
                "登录密码已更新。\n\n下次登录请使用新密码；"
                "如果在网页版或其它设备也登录着，建议一并重新登录。")

    def _open_change_email(self):
        """换绑登录邮箱（与系统设置页同一套流程）。"""
        from ..change_email import ChangeEmailDialog

        old_email = api.get_active_user() or ""
        dlg = ChangeEmailDialog(self, current_email=old_email)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.new_email:
            return
        new_email = dlg.new_email
        # 邮箱是唯一登录账号：本机「记住账号」必须跟着改
        saved_user, saved_pwd = db.get_remembered_login()
        if saved_user:
            db.save_remembered_login(new_email, saved_pwd,
                                     remember_user=True, remember_pwd=bool(saved_pwd))
        self.load()
        self.page.mw.refresh_status()
        QMessageBox.information(
            self, "换绑成功",
            f"登录邮箱已改为：\n{new_email}\n\n下次登录请使用新邮箱。")

    def _confirm_logout(self):
        ret = QMessageBox.question(
            self, "退出登录", "确定要退出当前账号并返回登录界面吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.page.mw.logout()
