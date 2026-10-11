"""iCalendar（RFC 5545）子集生成器 —— 把日程导出成能导进任何日历软件的 .ics。

**为什么不装第三方库**：桌面版是 PyInstaller onedir 分发，多一个依赖就多一份
要跟着 PyInstaller 打进去、要跟着 pyc 一起出问题的代码。而这里真正需要的只是
RFC5545 的一个极小子集（VCALENDAR + VEVENT + 少量属性），自己写反而更可控。

**这个文件里唯一的难点是「回读」**。.ics 出错的方式都很安静 —— 日历软件要么
直接忽略整条事件，要么把公司名从逗号处截成两半，而导出这边什么错都不会报。
所以这里刻意做了三件事，全部对着「别人读回来会怎样」而不是「字符串长什么样」：

1. **转义**（RFC5545 §3.3.11）：TEXT 值里的 ``\\`` ``,`` ``;`` 与换行必须转义。
   ``腾讯,深圳`` 不转义 ⇒ 日历里只剩「腾讯」。
2. **折叠**（§3.3.1）：单行 ≤ **75 个八位组**（不是字符），续行以**一个空格**开头。
   中文一字三字节 ⇒ 25 个汉字就超了；按字符数折叠会得到一行 225 字节的非法行。
   而且折叠**不能把多字节字符劈开**，否则续行开头是半个字，回读就是乱码。
3. **CRLF 行尾**：RFC 规定就是 CRLF。只写 LF 时主流日历软件能忍，但 Outlook
   与部分 Android 客户端会解析失败 —— 这种「在我机器上没问题」的坑最费时间。

时区处理刻意选择**不引入 VTIMEZONE**：定时事件用「浮动时间」（``DTSTART:20261007T143000``，
不带 ``Z`` 也不带 ``TZID``），日历软件会按用户本地时区解释 —— 这正是「面试 14:30
我在北京时间 14:30 到」的语义。DTSTAMP 这类**要求 UTC** 的字段则统一带 ``Z``。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

# 折叠前的单行上限（八位组）。RFC5545 §3.3.1 规定 75，不含 CRLF。
LINE_OCTET_LIMIT = 75

PRODID = "-//OfferFlow//Job Tracker//CN"
DEFAULT_CAL_NAME = "OfferFlow 求职日程"


def escape_text(value: str) -> str:
    """按 RFC5545 §3.3.11 转义 TEXT 值。

    顺序不能反：**必须先转义反斜杠**。否则 ``\\n`` 会被后面的换行替换再加工一次，
    得到 ``\\\\n``（回读出来是字面量 ``\\n``，不是换行）。
    """
    text = str(value or "")
    # 换行统一成 \n 再转义；CRLF / CR / LF 三种输入都要吃掉。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def _char_span(data: bytes, pos: int) -> int:
    """从 ``pos`` 开始的一个完整 UTF-8 字符占几个字节（按首字节推断，最多 4）。"""
    first = data[pos]
    if first < 0x80:
        n = 1
    elif first & 0xE0 == 0xC0:
        n = 2
    elif first & 0xF0 == 0xE0:
        n = 3
    elif first & 0xF8 == 0xF0:
        n = 4
    else:
        n = 1               # 非法首字节：按 1 字节走，行为可预测
    return max(1, min(n, len(data) - pos))


def fold_line(line: str, limit: int = LINE_OCTET_LIMIT) -> str:
    """把一行折成若干物理行（续行以单个空格开头）。

    按**八位组**切，任何情况下都不切开多字节字符 —— 这是本函数存在的全部理由。

    一条不能破的优先级：**40 字节的整齐是「应该」，不弄坏数据是「必须」。**
    所以当 ``limit`` 小到一个完整字符都放不下（只可能出现在极小 limit 下，
    真实场景 75 字节远大于任何 UTF-8 字符），宁可这一行超出 limit，
    也取出一个完整字符 —— 超长行顶多让严格的客户端抱怨，
    劈开的字符则会让摘要变成乱码，而且两边都不报错。
    """
    data = line.encode("utf-8")
    if len(data) <= limit:
        return line
    chunks: List[bytes] = []
    pos = 0
    budget = limit          # 首行可用 75 字节
    while pos < len(data):
        take = data[pos:pos + budget]
        # 末字节切在多字节字符中间时要退到最近的字符边界。
        # 这里**用 try/except 而不是手写位运算**：位运算得同时覆盖两种断点 ——
        #   ① 末字节是续接字节（10xxxxxx）→ 把续接字节退掉；
        #   ② 末字节是起始字节（11xxxxxx）但它的后续字节没进本轮 → 起始字节也要退掉。
        # 第一版只判了 ①，于是「长中文行正好在第 74 字节处切到某字的起始字节」时
        # 让不合法的 UTF-8 片段进了 chunks，最后 decode 直接 UnicodeDecodeError。
        # 交给解码器自己告诉我们哪里断，比手写位运算可靠得多。
        while take:
            try:
                take.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                if exc.start > 0:
                    take = take[:exc.start]
                    continue
                take = data[pos:pos + _char_span(data, pos)]
                break
        if not take:            # 兜底：至少前进 1 字节，避免死循环
            take = data[pos:pos + 1]
        chunks.append((b" " if chunks else b"") + take)
        pos += len(take)
        budget = limit - 1  # 续行要空出一位给前导空格
    return b"\r\n".join(chunks).decode("utf-8")


def _utc_stamp(moment: Optional[datetime] = None) -> str:
    """UTC 时间戳 ``YYYYMMDDTHHMMSSZ``（DTSTAMP / LAST-MODIFIED 用）。"""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _is_date_only(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _floating_stamp(moment: datetime) -> str:
    """浮动本地时间 ``YYYYMMDDTHHMMSS``（不带 Z / TZID，由日历软件按本地时区解释）。"""
    return moment.strftime("%Y%m%dT%H%M%S")


@dataclass
class CalendarEvent:
    """一条日程。

    ``when`` 可以是 ``date``（全天事件）或 ``datetime``（定时事件）。
    定时事件默认 1 小时时长，可用 ``duration`` 覆盖。
    """

    uid: str
    summary: str
    when: object
    description: str = ""
    location: str = ""
    categories: Sequence[str] = field(default_factory=tuple)
    duration: timedelta = timedelta(hours=1)
    all_day: Optional[bool] = None     # None = 按 when 的类型自动判定

    @property
    def is_all_day(self) -> bool:
        if self.all_day is not None:
            return bool(self.all_day)
        return _is_date_only(self.when)


def make_uid(*parts: object) -> str:
    """由业务键算出**确定性** UID。

    为什么不用 uuid4：同一个投递重复导出时必须得到同一个 UID —— 这样用户
    再次导入是「更新已有事件」，而不是在日历里堆出两份一模一样的面试。
    哈希只取前 20 位十六进制，足够避免同用户内的碰撞，又不会长得离谱。
    """
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"{digest}@offerflow"


def _event_lines(ev: CalendarEvent, stamp: str) -> List[str]:
    lines = ["BEGIN:VEVENT", f"UID:{escape_text(ev.uid)}", f"DTSTAMP:{stamp}"]
    if ev.is_all_day:
        day = ev.when if _is_date_only(ev.when) else ev.when.date()
        lines.append(f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
        # DTEND 对全天事件是**排他**的 ⇒ 单日事件的结束日是次日。
        lines.append(f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}")
    else:
        start = ev.when
        lines.append(f"DTSTART:{_floating_stamp(start)}")
        lines.append(f"DTEND:{_floating_stamp(start + ev.duration)}")
    lines.append(f"SUMMARY:{escape_text(ev.summary)}")
    if ev.description.strip():
        # 末尾换行会让部分客户端把 DESCRIPTION 当空值，先剥掉。
        lines.append(f"DESCRIPTION:{escape_text(ev.description.strip())}")
    if ev.location.strip():
        lines.append(f"LOCATION:{escape_text(ev.location)}")
    cats = [escape_text(c) for c in ev.categories if str(c).strip()]
    if cats:
        lines.append("CATEGORIES:" + ",".join(cats))
    lines.append("STATUS:CONFIRMED")
    # 求职日程不该把别人的「空闲」时段占掉，也不该出现在「忙碌」判定里。
    lines.append("TRANSP:TRANSPARENT")
    lines.append("END:VEVENT")
    return lines


def build_calendar(events: Iterable[CalendarEvent], *,
                   cal_name: str = DEFAULT_CAL_NAME,
                   prodid: str = PRODID,
                   now: Optional[datetime] = None) -> str:
    """生成完整 .ics 文本（CRLF 行尾、已折叠、末尾带 CRLF）。"""
    stamp = _utc_stamp(now)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(cal_name)}",
    ]
    for ev in events:
        lines.extend(_event_lines(ev, stamp))
    lines.append("END:VCALENDAR")
    folded: List[str] = []
    for line in lines:
        folded.extend(fold_line(line).split("\r\n"))
    return "\r\n".join(folded) + "\r\n"


def parse_calendar(text: str) -> List[dict]:
    """一个**最小** iCalendar 解析器 —— 存在的意义是给测试当判据，不是生产用。

    生产路径只需要「写」，不需要「读」；但「写对了没有」在离线环境里没有
    第三方库可以问，只能自己解一遍。所以这里刻意只实现回读所需的规则：

    * 先**反折叠**（续行 = 以空格或 TAb 开头的物理行，空格要去掉）；
    * 再解析 ``NAME;PARAM=...:VALUE``，参数只保留成 dict；
    * TEXT 值按 §3.3.11 **反转义**。

    它故意不实现 RRULE、VTIMEZONE、嵌套 VALARM —— 我们也不生成这些。
    谁要是将来给 builder 加了新结构，这个解析器会先炸，这正是想要的信号。
    """
    # ① 反折叠
    unfolded: List[str] = []
    for physical in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if physical.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += physical[1:]
        elif physical.strip():
            unfolded.append(physical)
    # ② 逐行
    events: List[dict] = []
    current: Optional[dict] = None
    # 用「见过」而不是「当前在不在里面」：合法的 .ics 最后一行就是
    # END:VCALENDAR，拿「解析结束时是否仍在 VCALENDAR 内」当判据会把好文件判死。
    saw_calendar = False
    for line in unfolded:
        name, params, value = _split_property(line)
        if name == "BEGIN" and value.upper() == "VCALENDAR":
            saw_calendar = True
            continue
        if name == "END" and value.upper() == "VCALENDAR":
            continue
        if name == "BEGIN" and value.upper() == "VEVENT":
            current = {"params": {}, "_order": []}
            continue
        if name == "END" and value.upper() == "VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        target = current if current is not None else None
        if target is None:
            continue
        target["_order"].append(name.upper())
        if params:
            target["params"][name.upper()] = params
        target[name.upper()] = unescape_text(value)
    if not saw_calendar:
        raise ValueError("不是合法的 iCalendar：缺少 BEGIN/END:VCALENDAR")
    for ev in events:
        ev.pop("_order", None)
    return events


def _split_property(line: str) -> Tuple[str, dict, str]:
    """``DTSTART;VALUE=DATE:20261007`` → ``("DTSTART", {"VALUE": "DATE"}, "20261007")``。

    注意分隔符顺序：**先找冒号再找分号** —— 值里可以出现分号（比如 URL），
    先切分号会把值一起切坏。
    """
    colon = line.find(":")
    if colon < 0:
        return line.strip().upper(), {}, ""
    head, value = line[:colon], line[colon + 1:]
    pieces = head.split(";")
    name = pieces[0].strip().upper()
    params: dict = {}
    for piece in pieces[1:]:
        if "=" in piece:
            k, v = piece.split("=", 1)
            params[k.strip().upper()] = v.strip()
    return name, params, value


def unescape_text(value: str) -> str:
    """反转义 TEXT 值（与 :func:`escape_text` 严格互逆）。"""
    out: List[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in ("\\", ";", ","):
                out.append(nxt)
            else:                       # 未知转义：原样保留，别吞字符
                out.append(ch)
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


__all__ = [
    "CalendarEvent", "build_calendar", "escape_text", "fold_line",
    "make_uid", "parse_calendar", "unescape_text", "LINE_OCTET_LIMIT",
    "DEFAULT_CAL_NAME", "PRODID",
]
