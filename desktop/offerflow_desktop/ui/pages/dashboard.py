"""首页：数据概览 + 流程引导 + 最近投递 / 匹配速览 + 快捷入口 + 提醒。

对齐网页版 Dashboard.tsx。两处「速览」都读本地优先的数据源：
  * 最近投递 → ``api_client.list_applications()``（离线时读本地副本）
  * 匹配速览 → ``api_client.list_matches()``（离线 / 接口不可用时返回空，整卡不显示）
"""

from datetime import date, datetime
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from ... import api_client as db
from ...config import STATUS_LABELS
from ..components import (
    card, hint_label, make_title, primary_button, score_badge, section_label,
    stat_card, status_chip, step_dot,
)

#: 「最近投递」显示条数（对齐网页版 `apps.slice(0, 6)`）
RECENT_LIMIT = 6
#: 「匹配速览」显示条数（对齐网页版 `rankings.slice(0, 4)`）
MATCH_LIMIT = 4


class DashboardPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        layout.addWidget(make_title(
            "首页",
            "求职全流程：生成简历 → 分析岗位 → 匹配评估 → 优化投递 → 面试准备 → 薪资谈判 → 追踪进度",
        ))

        # ===== 数据概览（白底卡片 + 细竖条点缀，2 行 3 列）=====
        # 统计只取一次（api_client 内部要打 4 个接口），再复用到流程步骤条
        stats = db.get_statistics()
        conv = stats.get("conversion_rate") or {}
        grid = QGridLayout()
        grid.setSpacing(12)
        cards = [
            ("我的简历", str(stats.get("resume_count", 0)), "#4f46e5", "已生成 / 优化"),
            ("已解析岗位", str(stats.get("jd_count", 0)), "#2563eb", "JD 库"),
            ("投递总数", str(stats.get("total", 0)), "#0891b2", "全流程追踪"),
            ("面经库存", str(stats.get("article_count", 0)), "#ea580c", "可生成面试题"),
            ("投递 → 面试率", f"{conv.get('applied_to_interview', 0)}%", "#16a34a", "转化"),
            ("面试 → Offer 率", f"{conv.get('interview_to_offer', 0)}%", "#db2777", "转化"),
        ]
        for i, (title, value, accent, sub) in enumerate(cards):
            grid.addWidget(stat_card(title, value, accent, sub), i // 3, i % 3)
        layout.addLayout(grid)

        # ===== 求职流程步骤条 =====
        layout.addWidget(self._flow_strip(stats))

        # ===== 快捷入口 =====
        quick = QHBoxLayout()
        quick.setSpacing(12)
        quick.addWidget(self._quick_card("没有简历？", "填写学校和经历，AI 生成一页内匹配岗位的中文简历", "开始生成", "简历生成"))
        quick.addWidget(self._quick_card("看到心仪岗位？", "粘贴 JD 文本，自动解析公司、硬性要求与技术栈", "解析 JD", "岗位分析"))
        quick.addWidget(self._quick_card("投简历前", "一键计算简历与岗位匹配度，看差距再决定投不投", "开始匹配", "岗位分析"))
        quick.addWidget(self._quick_card("临近面试？", "导入面经生成面试题，和 AI 模拟面试练一轮", "面试准备", "面试准备"))
        layout.addLayout(quick)

        # ===== 最近投递 + 匹配速览（与网页版首页同序）=====
        glance = QHBoxLayout()
        glance.setSpacing(12)
        glance.addWidget(self._recent_section(), 1)
        self.match_card = self._match_section()
        glance.addWidget(self.match_card, 1)
        layout.addLayout(glance)

        # ===== 待办提醒 =====
        layout.addWidget(self._reminder_section())
        layout.addStretch(1)

    # ================= 最近投递 / 匹配速览 =================

    def _recent_section(self) -> QWidget:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(section_label("最近投递"))
        head.addStretch(1)
        more = QPushButton("查看全部")
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.clicked.connect(lambda: self.mw.navigate("投递追踪"))
        head.addWidget(more)
        layout.addLayout(head)
        self.recent_host = QVBoxLayout()
        self.recent_host.setSpacing(4)
        layout.addLayout(self.recent_host)
        layout.addStretch(1)
        # 先渲染空态，真实数据在 showEvent 里补（构造期不发请求）
        self._render_recent([])
        return frame

    def _match_section(self) -> QWidget:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(section_label("匹配速览"))
        head.addStretch(1)
        more = QPushButton("查看全部")
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.clicked.connect(lambda: self.mw.navigate("岗位分析"))
        head.addWidget(more)
        layout.addLayout(head)
        self.match_host = QVBoxLayout()
        self.match_host.setSpacing(4)
        layout.addLayout(self.match_host)
        layout.addStretch(1)
        # 一条匹配记录都没有时整卡不占位（对齐网页版 `rankings.length > 0 &&`）
        frame.setVisible(False)
        return frame

    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render_recent(self, apps: List[dict]) -> None:
        self._clear(self.recent_host)
        if not apps:
            self.recent_host.addWidget(
                hint_label("暂无投递记录 —— 去「投递追踪」添加第一条，这里会显示最近 6 条。"))
            return
        for app in apps[:RECENT_LIMIT]:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(8)
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            company = app.get("company") or "（未填公司）"
            position = app.get("position") or "（未填岗位）"
            title = QLabel(f"{company} · {position}")
            title.setStyleSheet("font-size: 13px; font-weight: 600;")
            col.addWidget(title)
            col.addWidget(hint_label((app.get("updated_at") or "")[:10] or "—"))
            rl.addLayout(col, 1)
            rl.addWidget(status_chip(str(app.get("status") or "")))
            self.recent_host.addWidget(row)

    def _render_match(self, rankings: List[dict]) -> None:
        self._clear(self.match_host)
        if not rankings:
            self.match_card.setVisible(False)
            return
        self.match_card.setVisible(True)
        for item in rankings[:MATCH_LIMIT]:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(8)
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            title = QLabel(
                f"{item.get('company') or '未知公司'} · {item.get('position') or '未知职位'}")
            title.setStyleSheet("font-size: 13px; font-weight: 600;")
            col.addWidget(title)
            suggestion = hint_label(str(item.get("suggestion") or "")[:60] or "—")
            suggestion.setWordWrap(False)
            col.addWidget(suggestion)
            rl.addLayout(col, 1)
            rl.addWidget(score_badge(int(item.get("score") or 0)))
            self.match_host.addWidget(row)

    def _flow_strip(self, stats: dict) -> QWidget:
        frame = card()
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)
        steps = ["简历生成", "职位分析", "岗位分析", "简历优化", "面试准备", "薪资谈判", "投递追踪"]
        done_flags = [
            stats.get("resume_count", 0) > 0,
            stats.get("jd_count", 0) > 0,
            stats.get("total", 0) > 0,
            stats.get("optimized_count", 0) > 0,
            stats.get("article_count", 0) > 0,
            False,
            stats.get("total", 0) > 0,
        ]
        for i, step in enumerate(steps):
            layout.addWidget(step_dot(i + 1, step, done=done_flags[i]))
            if i < len(steps) - 1:
                arrow = QLabel("→")
                arrow.setStyleSheet("color: #c3c7d0; font-size: 13px;")
                layout.addWidget(arrow)
        layout.addStretch(1)
        return frame

    def _quick_card(self, title: str, desc: str, btn_text: str, page: str) -> QFrame:
        frame = card(highlight=True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        t = QLabel(title)
        t.setStyleSheet("font-size: 14px; font-weight: 600;")
        d = hint_label(desc)
        d.setWordWrap(True)
        btn = primary_button(btn_text)
        btn.clicked.connect(lambda: self.mw.navigate(page))
        layout.addWidget(t)
        layout.addWidget(d)
        layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        return frame

    def showEvent(self, event):
        """每次回到首页刷新提醒与两块速览。

        **不在构造期发请求**：主窗口会一次性构造全部页面，若页面在请求返回前
        被销毁（退出登录重建、页面切换），后台线程回调会摸到已删除的 C++ 对象，
        进程直接崩掉且无 traceback（已实测）。
        """
        super().showEvent(event)
        self._load_reminders()
        self._load_glance()

    def _load_glance(self) -> None:
        """刷新「最近投递」与「匹配速览」。

        两个数据源都是本地优先（`list_applications` 先拉后读本地、
        `list_matches` 失败返回空），所以断网时这里仍有内容、也不会抛。
        """
        self._render_recent(db.list_applications())
        self._render_match(db.list_matches())

    def _reminder_section(self) -> QWidget:
        frame = card(warn=True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        head = QHBoxLayout()
        title = QLabel("待办提醒")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        self.reminder_source_label = hint_label("")
        head.addWidget(self.reminder_source_label)
        self.reminder_refresh_btn = QPushButton("刷新")
        self.reminder_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reminder_refresh_btn.clicked.connect(self._load_reminders)
        head.addWidget(self.reminder_refresh_btn)
        layout.addLayout(head)

        self.reminder_host = QVBoxLayout()
        self.reminder_host.setSpacing(4)
        layout.addLayout(self.reminder_host)

        # 先用本机已有数据渲染（立即可见、断网也有内容），
        # 后端规则的数据在 showEvent 里异步拉取（后端按状态+天数算，比本地更准）
        self._render_reminders(self._local_reminder_rows(), "本机估算")
        return frame

    def _clear_reminders(self) -> None:
        while self.reminder_host.count():
            item = self.reminder_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render_reminders(self, rows: List[str], source: str = "") -> None:
        self._clear_reminders()
        self.reminder_source_label.setText(f"来源：{source}" if source else "")
        if not rows:
            self.reminder_host.addWidget(
                hint_label("暂无逾期跟进或 3 天内的待办事项，保持节奏"))
            return
        for row in rows[:8]:
            label = QLabel("• " + row)
            label.setWordWrap(True)
            label.setStyleSheet("font-size: 13px;")
            self.reminder_host.addWidget(label)

    def _local_reminder_rows(self) -> List[str]:
        """本机兜底：按本地数据粗算（断网 / 接口不可用时仍有内容）。"""
        rows: List[str] = []
        today = date.today()
        for app in db.list_applications():
            status = app.get("status") or ""
            if status in ("rejected", "accepted"):
                continue
            try:
                updated = datetime.fromisoformat(app.get("updated_at") or "").date()
                days_idle = (today - updated).days
            except (ValueError, TypeError):
                days_idle = 0
            company = app.get("company") or "（未填公司）"
            position = app.get("position") or "（未填岗位）"
            if days_idle >= 7:
                rows.append(
                    f"「{company} · {position}」已 {days_idle} 天未跟进"
                    f"（当前状态：{STATUS_LABELS.get(status, status or '未知')}）"
                )
            if app.get("next_action_date"):
                try:
                    nd = date.fromisoformat(app["next_action_date"])
                    delta = (nd - today).days
                    if 0 <= delta <= 3:
                        action = app.get("next_action") or "安排的行动"
                        rows.append(
                            f"「{company}」{action}（{nd.isoformat()}，"
                            f"{'今天' if delta == 0 else f'{delta} 天后'}）"
                        )
                except (ValueError, TypeError):
                    pass
        return rows

    def _load_reminders(self) -> None:
        """拉取后端算出的提醒（超期未跟进 + 即将到来的面试）。"""
        from ...llm import LLMWorker

        from ..components import api_guard

        self.reminder_refresh_btn.setEnabled(False)
        worker = LLMWorker(api_guard(db.list_reminders))
        worker.success.connect(self._on_reminders)
        worker.failed.connect(lambda _msg: self.reminder_refresh_btn.setEnabled(True))
        worker.start()
        self._reminder_worker = worker

    def _on_reminders(self, rem: dict) -> None:
        self.reminder_refresh_btn.setEnabled(True)
        if not isinstance(rem, dict) or rem.get("_error"):
            return  # 接口不可用就保留本机兜底内容
        rows: List[str] = []
        for it in rem.get("overdue") or []:
            status = it.get("status") or ""
            rows.append(
                f"「{it.get('company')} · {it.get('position')}」已 "
                f"{it.get('days_since_update', 0)} 天未跟进"
                f"（当前状态：{STATUS_LABELS.get(status, status or '未知')}）"
            )
        for it in rem.get("upcoming") or []:
            days = it.get("days_until")
            when = "今天" if days == 0 else f"{days} 天后"
            action = it.get("next_action") or "面试安排"
            rows.append(
                f"「{it.get('company')}」{action}"
                f"（{it.get('next_action_date') or ''}，{when}）"
            )
        self._render_reminders(rows, "云端规则")
