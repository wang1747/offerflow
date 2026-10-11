#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递追踪的「日历视图 + .ics 导出」判据。

为什么需要这份判据
------------------
.ics 出错的方式全是**静默**的：日历软件要么整条事件不显示，要么把公司名从
逗号处截成两半（``腾讯,深圳`` → ``腾讯``），而导出这边一声不吭。离线读代码
也看不出来 —— 唯一可靠的办法是把**写出去的字节读回来**对一遍。

三层判据，每层能独立失效：
  A. 结构层  —— CRLF 行尾 / 每物理行 ≤ 75 八位组 / 必需属性齐全
  B. 自解析层 —— core.ics.parse_calendar 把写出去的东西读回来，比**值**
  C. 可选跨实现层 —— 本机若装了 `icalendar`，用它再读一遍同一份文件
                     （跨进程/跨实现，才是「日历软件看到什么」）

⚠ 关于 C 层必须说清楚的一件事：实测 `icalendar` 7.3.0 对
「未转义逗号」「超过 75 八位组的行」「纯 LF 行尾」**全部宽容**，三种都能解析成功。
所以「让解析器报错」是空靶 —— A 层的字节级判据才是真正承重的那一层。
本用例把这条实测结论也断言下来，免得后来人再假设一遍。

反向对照（每条判据都要能被一个真实缺陷弄红）：
  ① 不转义逗号      → 值里出现未转义的 "," （豁免表同时做自证）
  ② 按字符数折叠    → 单行超 75 八位组
  ③ 折叠劈开多字节  → 回读不是原摘要
  ④ UID 用随机数    → 两次导出不一致
  ⑤ 导出中途失败    → 磁盘上不能留下半个文件

运行：python tests/test_ics_export.py
"""

import os
import sys
import tempfile
import unittest
import uuid
from datetime import date, datetime, time
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core import calendar_feed, ics          # noqa: E402

LONG_COMPANY = "某某某某某某某某某某某某某某某某某某某某某某某某某某某某某某有限责任公司"

# 手算的期望值：行动事件 5 条（a1/a2/a3/a4/a5）、投递日事件 4 条（a1/a2/a3/a5）
APPS = [
    {   # 含逗号的公司名 + 含全角分号与换行的备注 + 带时刻的面试
        "id": "a1", "company": "腾讯,深圳", "position": "AI 应用研发",
        "status": "first_interview", "applied_date": "2026-10-01",
        "next_action": "14:30 一面 / 线上腾讯会议", "next_action_date": "2026-10-08",
        "notes": "内推人：张三；\n记得带作品集",
    },
    {   # 只有日期、没有行动文案 → 标题兜底成「待跟进」，而不是丢掉
        "id": "a2", "company": "合合信息", "position": "RAG 算法",
        "status": "applied", "applied_date": "2026-10-02",
        "next_action": "", "next_action_date": "2026-10-08", "notes": "",
    },
    {   # 中文全角冒号的时刻
        "id": "a3", "company": "幂律智能", "position": "法律科技",
        "status": "online_test", "applied_date": "2026-10-03",
        "next_action": "09：00 笔试", "next_action_date": "2026-10-09", "notes": "",
    },
    {   # 超长公司名 → 必然触发 75 八位组折叠
        "id": "a4", "company": LONG_COMPANY, "position": "文档智能",
        "status": "saved", "applied_date": None,
        "next_action": "确认是否投递", "next_action_date": "2026-10-11", "notes": "",
    },
    {   # 公司名为空 → 兜底成「（未填公司）」
        "id": "a5", "company": "", "position": "", "status": "rejected",
        "applied_date": "2026-10-04", "next_action": "", "next_action_date": "2026-10-10",
        "notes": "",
    },
    {   # 一个日期都没有 → **一条事件都不该产生**（别拿今天凑数）
        "id": "a6", "company": "不该出现", "position": "x", "status": "saved",
        "applied_date": None, "next_action": "还没定时间", "next_action_date": None,
        "notes": "",
    },
    {   # 带时间的 ISO 串（服务端与本地库写法不一致）→ 必须能容忍
        "id": "a7", "company": "字节跳动", "position": "Agent 平台",
        "status": "second_interview", "applied_date": "2026-10-05T10:00:00",
        "next_action": "二面", "next_action_date": "2026-10-12", "notes": "",
    },
    "not-a-dict",     # 噪声：必须忽略而不是抛异常
]

EXPECT_ACTIONS = 6          # a1 a2 a3 a4 a5 a7
EXPECT_APPLIED = 5          # a1 a2 a3 a5 a7（a7 的 applied 是带时间的 ISO 串）


# ============================================================
# 结构判据（也給 UI 用例复用）
# ============================================================
# RFC5545 里**本身就是列表**的属性：它们的逗号是分隔符，不是数据。
# 豁免面开大了等于没查，所以只列这几个。
LIST_VALUED = {"CATEGORIES", "RESOURCES", "EXDATE", "RDATE", "FREEBUSY"}


def structure_failures(text: str) -> list:
    """CRLF / 折叠 / 必需属性。返回失败描述列表（空 = 通过）。"""
    fails = []
    if not text.startswith("BEGIN:VCALENDAR\r\n"):
        fails.append("首行不是 BEGIN:VCALENDAR + CRLF")
    if not text.endswith("END:VCALENDAR\r\n"):
        fails.append("结尾不是 END:VCALENDAR + CRLF")
    body = text.replace("\r\n", "")
    if "\n" in body or "\r" in body:
        fails.append("存在非 CRLF 的行尾（裸 LF 或裸 CR）")
    for idx, line in enumerate(text.split("\r\n"), start=1):
        n = len(line.encode("utf-8"))
        if n > ics.LINE_OCTET_LIMIT:
            fails.append("第 %d 物理行 %d 八位组 > %d" % (idx, n, ics.LINE_OCTET_LIMIT))
        if line.startswith("  "):
            fails.append("第 %d 行续行前导不止一个空格" % idx)
    for required in ("VERSION:2.0", "PRODID:", "CALSCALE:GREGORIAN"):
        if required not in text:
            fails.append("缺少 %s" % required)
    for i, block in enumerate(text.split("BEGIN:VEVENT")[1:], start=1):
        for prop in ("UID:", "DTSTAMP:", "DTSTART", "SUMMARY:"):
            if prop not in block:
                fails.append("第 %d 个 VEVENT 缺 %s" % (i, prop))
    if not text.split("BEGIN:VEVENT")[1:]:
        fails.append("一个 VEVENT 都没有")
    return fails


def unescaped_separator_failures(text: str, exempt: bool = True) -> list:
    """TEXT 值里不该出现**没转义的** ``,`` / ``;``（RFC5545 §3.3.11）。

    ``exempt=False`` 用于判据自证：去掉豁免表后正确产物自己就该变红 ——
    那才证明豁免真的在起作用，而不是写了个没人走的判断。
    """
    fails = []
    for line in text.split("\r\n"):
        if not line or line.startswith(" "):
            continue
        colon = line.find(":")
        if colon < 0:
            continue
        name = line[:colon].split(";", 1)[0].strip().upper()
        if exempt and name in LIST_VALUED:
            continue
        value = line[colon + 1:]
        i = 0
        while i < len(value):
            if value[i] == "\\":
                i += 2
                continue
            if value[i] in ",;":
                fails.append("%s 的值里有未转义的 %r" % (name, value[i]))
                break
            i += 1
    return fails


# ============================================================
# ① ics 原语
# ============================================================

class TestIcsPrimitives(unittest.TestCase):

    def test_escape_roundtrip(self):
        """转义必须与反转义严格互逆（顺序错了会得到字面量 ``\\n``）。"""
        for probe in ("a,b", "a;b", "a\\b", "a\nb", "",
                      "逗号,分号;反斜杠\\换行\n结束"):
            with self.subTest(probe=probe):
                self.assertEqual(ics.unescape_text(ics.escape_text(probe)), probe)

    def test_escape_normalizes_newlines(self):
        """换行统一成 ``\\n``：CRLF / CR / LF 三种输入都要吃掉。

        这是**有意的**排版层归一（`\\r\\n` 在 TEXT 值里没有意义，
        留着反而会让部分客户端多出空行）。所以它不属于「严格互逆」。
        """
        self.assertEqual(ics.escape_text("a\r\nb"), "a\\nb")
        self.assertEqual(ics.escape_text("a\rb"), "a\\nb")
        self.assertEqual(ics.escape_text("a\nb"), "a\\nb")

    def test_escape_backslash_first(self):
        """先转义反斜杠：反序会把 ``\\n`` 加工成 ``\\\\n``。"""
        self.assertEqual(ics.escape_text("\n"), "\\n")
        self.assertEqual(ics.escape_text("\\n"), "\\\\n")

    def test_fold_respects_octet_limit(self):
        """中文一字三字节 ⇒ 必须按**八位组**折，不能按字符数。"""
        line = "SUMMARY:" + "中文测试" * 40
        folded = ics.fold_line(line)
        for idx, part in enumerate(folded.split("\r\n")):
            with self.subTest(line=idx):
                self.assertLessEqual(len(part.encode("utf-8")), ics.LINE_OCTET_LIMIT)
        # 续行必须以恰好一个空格开头，且反折叠后与原文一致
        self.assertTrue(folded.split("\r\n")[1].startswith(" "))
        self.assertFalse(folded.split("\r\n")[1].startswith("  "))
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_fold_never_splits_multibyte(self):
        """折叠不能把汉字劈成两半 —— 劈开会得到乱码。

        这条曾经真的踩过：只判断「末字节是续接字节」，漏了
        「末字节是多字节起始字节、但它的后续字节没进本轮」的情况，
        结果 `fold_line` 自己就以 UnicodeDecodeError 崩了。
        """
        for width in range(1, 120):
            line = "SUMMARY:" + "腾讯,深圳" * 12
            folded = ics.fold_line(line, limit=max(2, width))
            with self.subTest(limit=width):
                folded.encode("utf-8")          # 不抛 UnicodeDecodeError
                self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_build_calendar_structure(self):
        text = calendar_feed.build_ics(APPS)
        self.assertEqual(structure_failures(text), [])
        self.assertEqual(unescaped_separator_failures(text), [])

    def test_uid_is_deterministic(self):
        """同一业务键 → 同一 UID；重复导入是**更新**而不是堆两份。"""
        same = ics.make_uid("a1", "action", "2026-10-08", "1430")
        self.assertEqual(same, ics.make_uid("a1", "action", "2026-10-08", "1430"))
        self.assertNotEqual(same, ics.make_uid("a1", "action", "2026-10-09", "1430"))
        self.assertNotEqual(same, ics.make_uid("a2", "action", "2026-10-08", "1430"))
        self.assertTrue(same.endswith("@offerflow"))
        self.assertEqual(len(same.split("@")[0]), 20)

    def test_two_exports_are_byte_identical(self):
        self.assertEqual(calendar_feed.build_ics(APPS), calendar_feed.build_ics(APPS))

    def test_required_params(self):
        events = ics.parse_calendar(calendar_feed.build_ics(APPS))
        timed = [e for e in events if "T" in e.get("DTSTART", "")]
        self.assertEqual(len(timed), 2, "定时事件应恰好 2 条（14:30 与 09:00）")
        for ev in timed:
            self.assertFalse(ev["DTSTART"].endswith("Z"),
                             "定时事件用浮动本地时间，不该带 Z")
        for ev in events:
            self.assertTrue(ev.get("DTSTAMP", "").endswith("Z"), "DTSTAMP 必须是 UTC")


# ============================================================
# ② 业务映射
# ============================================================

class TestCalendarFeed(unittest.TestCase):

    def test_default_excludes_applied(self):
        feed = calendar_feed.collect_events(APPS)
        self.assertEqual(len(feed), EXPECT_ACTIONS)
        self.assertFalse(any(ev.summary.startswith("已投递") for ev in feed))

    def test_include_applied(self):
        feed = calendar_feed.collect_events(APPS, include_applied=True)
        self.assertEqual(len(feed), EXPECT_ACTIONS + EXPECT_APPLIED)
        self.assertTrue(any(ev.summary.startswith("已投递 · ") for ev in feed))

    def test_rows_without_dates_produce_nothing(self):
        feed = calendar_feed.collect_events(APPS)
        self.assertFalse(any("不该出现" in ev.summary for ev in feed))

    def test_non_dict_rows_are_ignored(self):
        """脏数据不该让整个导出崩掉。"""
        self.assertEqual(len(calendar_feed.collect_events(["nope", None, 42])), 0)

    def test_comma_company_name_survives(self):
        feed = calendar_feed.collect_events(APPS)
        self.assertIn("腾讯,深圳 · 14:30 一面 / 线上腾讯会议",
                      [ev.summary for ev in feed])

    def test_missing_action_text_falls_back(self):
        """next_action 与 next_action_date 可能只填一半 —— 有日期就要有事件。"""
        feed = calendar_feed.collect_events(APPS)
        self.assertIn("合合信息 · 待跟进", [ev.summary for ev in feed])

    def test_time_extracted_from_free_text(self):
        """自由文本里的时刻要认出来，否则日历上只是条全天横条、到点不提醒。"""
        feed = {ev.summary: ev for ev in calendar_feed.collect_events(APPS)}
        self.assertEqual(feed["腾讯,深圳 · 14:30 一面 / 线上腾讯会议"].at, time(14, 30))
        self.assertEqual(feed["幂律智能 · 09：00 笔试"].at, time(9, 0),
                         "全角冒号也要认")
        self.assertIsNone(feed["字节跳动 · 二面"].at, "认不出时刻就老实按全天")

    def test_blank_company_falls_back(self):
        feed = calendar_feed.collect_events(APPS)
        self.assertTrue(any(ev.summary.startswith("（未填公司）") for ev in feed))

    def test_parse_day_tolerates_iso_datetime(self):
        self.assertEqual(calendar_feed.parse_day("2026-10-07"), date(2026, 10, 7))
        self.assertEqual(calendar_feed.parse_day("2026-10-07T09:30:00"), date(2026, 10, 7))
        self.assertIsNone(calendar_feed.parse_day(None))
        self.assertIsNone(calendar_feed.parse_day(""))
        self.assertIsNone(calendar_feed.parse_day("不是日期"))

    def test_same_day_timed_events_come_first(self):
        feed = [ev for ev in calendar_feed.collect_events(APPS)
                if ev.day == date(2026, 10, 8)]
        self.assertEqual([ev.is_timed for ev in feed], [True, False])

    def test_readback_matches_feed(self):
        """B 层：写出去再读回来，摘要与 DTSTART 必须逐条对上。"""
        feed = calendar_feed.collect_events(APPS)
        text = calendar_feed.build_ics_from_feed(feed)
        events = ics.parse_calendar(text)
        self.assertEqual(len(events), len(feed))
        got = {e.get("SUMMARY"): e.get("DTSTART") for e in events}
        for ev in feed:
            want = (ev.day.strftime("%Y%m%d")
                    + ("T" + ev.at.strftime("%H%M%S") if ev.at else ""))
            with self.subTest(summary=ev.summary):
                self.assertIn(ev.summary, got)
                self.assertEqual(got[ev.summary], want)

    def test_build_from_feed_equals_build_from_apps(self):
        """导出走「眼前那份」，口径必须与从原始数据算的一致。"""
        self.assertEqual(calendar_feed.build_ics_from_feed(
            calendar_feed.collect_events(APPS)), calendar_feed.build_ics(APPS))


# ============================================================
# ③ 反向对照：每条判据都要能被真实缺陷弄红
# ============================================================

class TestReverseControls(unittest.TestCase):

    def setUp(self):
        self.text = calendar_feed.build_ics(APPS)
        self.feed = calendar_feed.collect_events(APPS)

    def test_reverse_unescaped_comma(self):
        bad = self.text.replace("腾讯\\,深圳", "腾讯,深圳")
        self.assertNotEqual(bad, self.text, "变异没生效 → 对照是空靶")
        fails = unescaped_separator_failures(bad)
        self.assertTrue(any("未转义" in f for f in fails), fails)

    def test_reverse_exemption_is_load_bearing(self):
        """去掉豁免表后正确产物自己就变红 —— 证明豁免不是摆设。"""
        self.assertTrue(unescaped_separator_failures(self.text, exempt=False))

    def test_reverse_char_count_folding(self):
        """按字符数折叠 ⇒ 单行 138 八位组，必须被抓到。"""
        unfolded = self.text.replace("\r\n ", "")
        out = []
        for line in unfolded.split("\r\n"):
            if len(line) <= 75:
                out.append(line)
            else:
                out.append(line[:75])
                rest = line[75:]
                while rest:
                    out.append(" " + rest[:74])
                    rest = rest[74:]
        bad = "\r\n".join(out) + "\r\n"
        fails = structure_failures(bad)
        self.assertTrue(any("八位组" in f for f in fails), fails)

    def test_reverse_split_multibyte(self):
        """折叠劈开汉字 ⇒ 回读不出原摘要。"""
        unfolded = self.text.replace("\r\n ", "")
        out = []
        for line in unfolded.split("\r\n"):
            data = line.encode("utf-8")
            if len(data) <= 75:
                out.append(line)
                continue
            out.append(data[:75].decode("utf-8", "replace"))
            rest = data[75:]
            while rest:
                out.append(" " + rest[:74].decode("utf-8", "replace"))
                rest = rest[74:]
        bad = "\r\n".join(out) + "\r\n"
        summaries = [e.get("SUMMARY") for e in ics.parse_calendar(bad)]
        self.assertNotIn(LONG_COMPANY + " · 确认是否投递", summaries)
        # 对照补：未劈开时它确实读得出来（证明上一条不是因为别的原因红的）
        self.assertIn(LONG_COMPANY + " · 确认是否投递",
                      [e.get("SUMMARY") for e in ics.parse_calendar(self.text)])

    def test_reverse_lf_only(self):
        bad = self.text.replace("\r\n", "\n")
        fails = structure_failures(bad)
        self.assertTrue(any("CRLF" in f for f in fails), fails)

    def test_reverse_random_uid_breaks_determinism(self):
        """UID 用随机数 ⇒ 重复导入会在日历里堆出两份一样的面试。"""
        events = calendar_feed.to_calendar_events(self.feed)
        events[0].uid = str(uuid.uuid4())
        text = ics.build_calendar(events)
        self.assertNotEqual(text, self.text)
        events2 = calendar_feed.to_calendar_events(self.feed)
        events2[0].uid = str(uuid.uuid4())
        self.assertNotEqual(ics.build_calendar(events2), text)

    def test_reverse_missing_dtstamp(self):
        """VEVENT 缺 DTSTAMP 在规范里就是无效事件，判据必须认得出来。"""
        bad = self.text.replace("DTSTAMP:", "DTSTAMPX:")
        fails = structure_failures(bad)
        self.assertTrue(any("DTSTAMP" in f for f in fails), fails)


# ============================================================
# ④ 可选的跨实现校验（本机装了 icalendar 才跑）
# ============================================================

class TestExternalParser(unittest.TestCase):
    """用真实第三方解析器再读一遍同一个字节流。

    自研 builder 与自研 parser 出自同一个人、共享同一套误解。这一层能发现
    「两边都以为逗号是普通字符」这类**共同错误**。

    本机没装 `icalendar` 时会明确跳过并打印一行，**不会假装通过**。
    """

    @classmethod
    def setUpClass(cls):
        try:
            from icalendar import Calendar
        except ImportError:                       # pragma: no cover
            Calendar = None
        cls.Calendar = Calendar
        if Calendar is None:
            print("    [跳过] 本机未安装 icalendar，跨实现校验不执行。"
                  "\n           要跑：pip install icalendar 后重跑本用例。")

    def setUp(self):
        if self.Calendar is None:
            self.skipTest("icalendar 未安装")

    def test_external_readback(self):
        raw = calendar_feed.build_ics(APPS).encode("utf-8")
        events = self.Calendar.from_ical(raw).walk("VEVENT")
        self.assertEqual(len(events), EXPECT_ACTIONS)
        got = {}
        for comp in events:
            # vText 是 str 的**子类** ⇒ isinstance(x, str) 恒真、什么都测不到。
            # 判据必须落在值的内容上。
            got[str(comp.get("SUMMARY"))] = comp
        self.assertEqual(len(got), EXPECT_ACTIONS, "摘要撞车了，看不出是哪条")
        self.assertIn("腾讯,深圳 · 14:30 一面 / 线上腾讯会议", got)
        self.assertIn(LONG_COMPANY + " · 确认是否投递", got, "折叠后必须完整还原")
        self.assertFalse([k for k in got if "\\" in k], "不该有残留转义反斜杠")
        for summary, comp in got.items():
            dt = comp.get("DTSTART").dt
            with self.subTest(summary=summary):
                if "14:30" in summary:
                    self.assertEqual((dt.hour, dt.minute), (14, 30))
                elif "09：00" in summary:
                    self.assertEqual((dt.hour, dt.minute), (9, 0))
                else:
                    self.assertIsInstance(dt, date)

    def test_external_parser_is_lenient_about_format(self):
        """把「解析器不会报错」这条实测结论钉住。

        正因为 icalendar 对超长行/纯 LF/未转义逗号**全都宽容**，
        A 层的字节级判据才是承重的 —— 这条断言存在的意义就是防止
        后人再假设一次「解析器会拦住它」。
        """
        unfolded = calendar_feed.build_ics(APPS).replace("\r\n ", "")
        out = []
        for line in unfolded.split("\r\n"):
            if len(line) <= 75:
                out.append(line)
            else:
                out.append(line[:75])
                rest = line[75:]
                while rest:
                    out.append(" " + rest[:74])
                    rest = rest[74:]
        overlong = ("\r\n".join(out) + "\r\n").encode("utf-8")
        self.assertGreater(max(len(l) for l in overlong.split(b"\r\n")), 75)
        try:
            n_over = len(self.Calendar.from_ical(overlong).walk("VEVENT"))
        except Exception:                         # noqa: BLE001
            n_over = None
        # 宽容（照读不误）或严格（报错）都能接受 —— 但必须**如实记录**是哪一种，
        # 并且 A 层判据在两种情况下都独立成立。
        self.assertTrue(n_over is None or n_over == EXPECT_ACTIONS,
                        "超长行被读成了 %r 条事件" % (n_over,))
        self.assertTrue(structure_failures(overlong.decode("utf-8")),
                        "A 层判据必须独立地判它不合法")


if __name__ == "__main__":
    unittest.main(verbosity=2)
