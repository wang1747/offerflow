"""把投递记录翻译成日程 —— ics.py 只懂 iCalendar，业务语义全在这里。

**为什么单开一个模块**：``core/ics.py`` 是通用的（换个产品照样能用），
这一层才知道「投递记录长什么样」「什么算一次面试」。混在一起的话，
ics.py 就会开始 import config 里的 STATUS_LABELS，再也没法单独测。

日历上到底放什么，是**有取舍**的：

* ``next_action_date`` + ``next_action``  → 一条「待办/面试」事件。**默认导出**，
  这是用户真正要的东西（"面试/截止时间落格"）。
* ``applied_date`` → 一条「已投递」事件。**默认不导出** —— 投递日几乎天天都有，
  全铺上去会把日历糊成一片，真正要看的面试反而淹没在里面。UI 里给开关。

另外一个容易忽略的点：``next_action`` 是自由文本（"14:30 二面 / 线上腾讯会议"），
里面**常常带着时刻**。如果一律按全天事件导出，日历上就只显示一条横条，
用户到点收不到提醒 —— 所以这里会从文本里认一个 ``HH:MM``，认出来就导成
定时事件。认不出来（或认出的时刻不合理）就老实按全天，不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ics import CalendarEvent, build_calendar, make_uid

# 业务事件类型
KIND_ACTION = "action"      # 下一步行动 / 面试 / 截止
KIND_APPLIED = "applied"    # 投递当天

# 匹配 "14:30" / "9:05" / "14：30"（中文全角冒号很常见，不能漏）
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)(?!\d)")


def parse_day(value) -> Optional[date]:
    """宽容地解析 ``"2026-10-07"`` / ``"2026-10-07T09:00:00"`` → ``date``。

    投递记录里这个字段可能是 None、空串、或带时间的 ISO 串（服务端与本地库
    两边的写法不完全一致），所以不能直接 ``date.fromisoformat`` —— 那会在
    遇到带时间的串时抛异常，把整个导出打断。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    elif " " in text:
        text = text.split(" ", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_time(value) -> Optional[Tuple[int, int]]:
    """从自由文本里认一个 ``(时, 分)``；认不出返回 None。"""
    match = _TIME_RE.search(str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class FeedEvent:
    """一条日程（业务视角，还没变成 iCalendar）。"""

    app_id: str
    kind: str
    day: date
    summary: str
    detail: str
    status: str = ""
    at: Optional[time] = None

    @property
    def is_timed(self) -> bool:
        return self.at is not None

    @property
    def uid(self) -> str:
        """确定性 UID：同一投递的同一类事件重复导出 → 同一条日历事件。

        掺进 ``day`` 与 ``at`` 是有意的：改了面试日期应当**新开**一条，
        而不是把旧日期那条悄悄挪走 —— 万一用户已经在那条上加了备注，
        悄悄挪走比留下来更让人困惑。
        """
        return make_uid(self.app_id, self.kind, self.day.isoformat(),
                        "" if self.at is None else self.at.strftime("%H%M"))


def _detail_block(app: dict, next_action: str = "") -> str:
    lines: List[str] = []
    position = str(app.get("position") or "").strip()
    if position:
        lines.append(f"岗位：{position}")
    status = str(app.get("status") or "").strip()
    if status:
        lines.append(f"状态：{status}")
    applied = parse_day(app.get("applied_date"))
    if applied:
        lines.append(f"投递日期：{applied.isoformat()}")
    if next_action:
        lines.append(f"下一步：{next_action}")
    notes = str(app.get("notes") or "").strip()
    if notes:
        lines.append(f"备注：{notes}")
    return "\n".join(lines)


def collect_events(apps: Iterable[dict], *,
                   include_applied: bool = False,
                   include_actions: bool = True) -> List[FeedEvent]:
    """把投递列表摊成日程列表（已排序：先按日期，同日定时事件在前、全天在后）。"""
    from ..config import STATUS_LABELS

    feed: List[FeedEvent] = []
    for app in apps or []:
        if not isinstance(app, dict):
            continue
        app_id = str(app.get("id") or "")
        company = str(app.get("company") or "").strip() or "（未填公司）"
        status = str(app.get("status") or "")
        status_label = STATUS_LABELS.get(status, status)

        if include_actions:
            day = parse_day(app.get("next_action_date"))
            if day:
                action = str(app.get("next_action") or "").strip()
                # next_action 与 next_action_date 两个字段在库里都可能缺一半，
                # 只要日期在就导出一条 —— 标题兜底成「待跟进」而不是丢掉。
                summary = f"{company} · {action}" if action else f"{company} · 待跟进"
                clock = parse_time(action)
                feed.append(FeedEvent(
                    app_id=app_id, kind=KIND_ACTION, day=day, summary=summary,
                    detail=_detail_block(app, action), status=status_label,
                    at=time(clock[0], clock[1]) if clock else None,
                ))

        if include_applied:
            day = parse_day(app.get("applied_date"))
            if day:
                position = str(app.get("position") or "").strip()
                summary = f"已投递 · {company}" + (f"（{position}）" if position else "")
                feed.append(FeedEvent(
                    app_id=app_id, kind=KIND_APPLIED, day=day, summary=summary,
                    detail=_detail_block(app), status=status_label, at=None,
                ))

    def sort_key(ev: FeedEvent):
        # 有时刻的排在前面并把时刻带上；全天的排在当天最后（at=None → 23:59 之后）。
        return (ev.day, (ev.at.hour * 60 + ev.at.minute) if ev.at else 10 ** 6, ev.summary)

    feed.sort(key=sort_key)
    return feed


def to_calendar_events(feed: Sequence[FeedEvent]) -> List[CalendarEvent]:
    """把业务日程翻成 iCalendar 事件。"""
    out: List[CalendarEvent] = []
    for ev in feed:
        when = datetime.combine(ev.day, ev.at) if ev.at else ev.day
        out.append(CalendarEvent(
            uid=ev.uid, summary=ev.summary, when=when, description=ev.detail,
            categories=[c for c in ("求职", ev.status) if c],
        ))
    return out


def group_by_day(feed: Sequence[FeedEvent]) -> Dict[date, List[FeedEvent]]:
    """按日期分组（日历视图落格要用）。"""
    grouped: Dict[date, List[FeedEvent]] = {}
    for ev in feed:
        grouped.setdefault(ev.day, []).append(ev)
    return grouped


def build_ics_from_feed(feed: Sequence[FeedEvent], *,
                        cal_name: str = "OfferFlow 求职日程") -> str:
    """由**已经算好的日程**生成 .ics。

    为什么要有这个入口，而不是让调用方再去 collect 一遍：
    导出必须导「用户眼前看到的那份」。如果导出时重新读一次数据，中间任何一次
    后台同步或并发改动，都会让**文件与日历显示不一致** —— 用户会拿到一份
    自己没见过的日程，而且没有任何提示。
    """
    return build_calendar(to_calendar_events(feed), cal_name=cal_name)


def build_ics(apps: Iterable[dict], *,
              include_applied: bool = False,
              include_actions: bool = True,
              cal_name: str = "OfferFlow 求职日程") -> str:
    """一步到位：投递列表 → .ics 文本。"""
    feed = collect_events(apps, include_applied=include_applied,
                          include_actions=include_actions)
    return build_ics_from_feed(feed, cal_name=cal_name)


__all__ = [
    "FeedEvent", "KIND_ACTION", "KIND_APPLIED", "build_ics", "build_ics_from_feed",
    "collect_events", "group_by_day", "parse_day", "parse_time", "to_calendar_events",
]
