#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递追踪「日历视图 + 导出 .ics」的 **UI 层**判据。

为什么单独一个文件：`test_ics_export.py` 刻意不依赖 PySide6，
这样它能在任何装了 `icalendar` 的解释器下跑跨实现校验；
UI 判据需要 Qt，放在这里互不牵连。

UI 判据必须是**控件状态**，不是「代码里写了这行」：
  · 翻到某一天，右侧列表到底有几条、第一条是不是 14:30；
  · 日期格子上到底有没有加粗记号（`setDateTextFormat` 是累积的，
    不清旧记号就会出现「筛掉的状态还留着颜色」——最容易假绿的地方）；
  · 点「导出」之后**磁盘上真的落了一个文件**，而且能读回原样。

反向对照：
  ① 脏数据 → 日历降级成空且**给出失败说明**（提示不能被「无安排」覆盖）
  ② 生成失败 → 弹警告且**不落半个文件**
  ③ 空日程 → 拦下来，不导出空日历
  ④ 行号使用者共用同一份列表（不重复联网、不按行号取错记录）

运行：python tests/test_tracker_calendar_ui.py
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QDate                                # noqa: E402
from PySide6.QtGui import QFont                                 # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.core import calendar_feed, ics           # noqa: E402
from offerflow_desktop.ui.pages import tracker                  # noqa: E402

LONG_COMPANY = "某某某某某某某某某某某某某某某某某某某某某某某某某某某某某某有限责任公司"

APPS = [
    {"id": "a1", "company": "腾讯,深圳", "position": "AI 应用研发",
     "status": "first_interview", "applied_date": "2026-10-01",
     "next_action": "14:30 一面 / 线上腾讯会议", "next_action_date": "2026-10-08",
     "notes": "内推人：张三；\n记得带作品集", "updated_at": "2026-10-01T10:00:00"},
    {"id": "a2", "company": "合合信息", "position": "RAG 算法",
     "status": "applied", "applied_date": "2026-10-02",
     "next_action": "", "next_action_date": "2026-10-08",
     "notes": "", "updated_at": "2026-10-02T10:00:00"},
    {"id": "a3", "company": "幂律智能", "position": "法律科技",
     "status": "online_test", "applied_date": "2026-10-03",
     "next_action": "09：00 笔试", "next_action_date": "2026-10-09",
     "notes": "", "updated_at": "2026-10-03T10:00:00"},
    {"id": "a4", "company": LONG_COMPANY, "position": "文档智能",
     "status": "saved", "applied_date": None,
     "next_action": "确认是否投递", "next_action_date": "2026-10-11",
     "notes": "", "updated_at": "2026-10-04T10:00:00"},
    {"id": "a5", "company": "", "position": "", "status": "rejected",
     "applied_date": "2026-10-04", "next_action": "", "next_action_date": "2026-10-10",
     "notes": "", "updated_at": "2026-10-05T10:00:00"},
    {"id": "a6", "company": "不该出现", "position": "x", "status": "saved",
     "applied_date": None, "next_action": "还没定时间", "next_action_date": None,
     "notes": "", "updated_at": "2026-10-06T10:00:00"},
]

# 手算：行动事件 5 条（a1/a2/a3/a4/a5），投递日事件 4 条（a1/a2/a3/a5）
EXPECT_ACTIONS = 5
EXPECT_APPLIED = 4
# 行动日 4 个（10-08 两条 / 10-09 / 10-10 / 10-11），投递日 4 个（10-01..10-04）
EXPECT_ACTION_DAYS = 4
EXPECT_ALL_DAYS = 8


class _FakeMW(QWidget):
    def refresh_status(self):
        pass

    def navigate(self, *a, **k):
        pass

    def open_agent(self, *a, **k):
        pass


def qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


class _TrackerCase(unittest.TestCase):
    """公共装置：打桩数据源与两个模态框（否则用例会卡在真对话框上）。"""

    def setUp(self):
        self._patches = [
            mock.patch.object(tracker.db, "list_applications",
                              side_effect=lambda: [dict(a) for a in APPS]),
            mock.patch.object(tracker, "QMessageBox"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.mw = _FakeMW()
        self.page = tracker.TrackerPage(self.mw)
        self.addCleanup(self.page.setParent, None)

    def messages(self, kind):
        """取出被弹出来的对话框 (title, body) 列表。"""
        box = tracker.QMessageBox
        out = []
        for call in getattr(box, kind).call_args_list:
            args = call[0][1:]
            out.append((args[0], args[1] if len(args) > 1 else ""))
        return out


class TestCalendarView(_TrackerCase):

    def test_tabs(self):
        self.assertEqual(self.page.tabs.count(), 3)
        self.assertEqual([self.page.tabs.tabText(i) for i in range(3)],
                         ["看板与列表", "日历", "统计"])

    def test_default_scope_excludes_applied(self):
        self.assertFalse(self.page.cal_include_applied.isChecked(),
                         "默认应只导「下一步行动/面试」")
        self.assertEqual(len(self.page._feed), EXPECT_ACTIONS)
        self.assertFalse(any(ev.summary.startswith("已投递") for ev in self.page._feed))

    def test_undated_row_never_reaches_calendar(self):
        self.assertFalse(any("不该出现" in ev.summary for ev in self.page._feed))

    def test_table_and_calendar_share_one_source(self):
        """表格列出**全部投递**（6 条），日历只列**有日期的事件**（5 条）。

        两者条数本来就不同，所以「同源」不能拿条数相等来证 —— 要证的是
        「行号 → 记录」的映射与表格里显示的**是同一份数据**。
        """
        self.assertEqual(self.page.table.rowCount(), len(APPS))
        self.assertEqual(len(self.page._feed), EXPECT_ACTIONS)
        for row in range(self.page.table.rowCount()):
            self.page.table.selectRow(row)
            shown = self.page.table.item(row, 0).text()
            self.assertEqual(self.page._selected_app()["company"], shown,
                             "第 %d 行显示的与按行号取到的不是同一条" % row)

    def test_day_marks(self):
        marked = {d.toPython() for d in self.page._marked_dates}
        self.assertEqual(marked, {date(2026, 10, 8), date(2026, 10, 9),
                                  date(2026, 10, 10), date(2026, 10, 11)})
        self.assertEqual(self.page.calendar.dateTextFormat(
            qdate(date(2026, 10, 8))).fontWeight(), QFont.Weight.Bold)
        self.assertNotEqual(self.page.calendar.dateTextFormat(
            qdate(date(2026, 10, 20))).fontWeight(), QFont.Weight.Bold)

    def test_select_day_lists_events(self):
        self.page.calendar.setSelectedDate(qdate(date(2026, 10, 8)))
        self.assertEqual(self.page.cal_list.count(), 2)
        self.assertTrue(self.page.cal_list.item(0).text().startswith("14:30"),
                        "定时事件应排在前面且带时刻")
        self.assertTrue(self.page.cal_list.item(1).text().startswith("全天"))
        self.assertIn("腾讯,深圳", self.page.cal_list.item(0).text())
        self.assertIn("2 项", self.page.cal_day_label.text())

    def test_long_company_name_survives_to_ui(self):
        self.page.calendar.setSelectedDate(qdate(date(2026, 10, 11)))
        self.assertEqual(self.page.cal_list.count(), 1)
        self.assertIn(LONG_COMPANY, self.page.cal_list.item(0).text())

    def test_empty_day_is_distinguishable_from_failure(self):
        self.page.calendar.setSelectedDate(qdate(date(2026, 10, 20)))
        self.assertEqual(self.page.cal_list.count(), 0)
        self.assertIn("无安排", self.page.cal_day_label.text())
        self.assertEqual(self.page.cal_hint.text(), "")

    def test_applied_toggle_updates_calendar(self):
        self.page.cal_include_applied.setChecked(True)
        self.assertEqual(len(self.page._feed), EXPECT_ACTIONS + EXPECT_APPLIED)
        marked = {d.toPython() for d in self.page._marked_dates}
        self.assertEqual(len(marked), EXPECT_ALL_DAYS)
        self.page.calendar.setSelectedDate(qdate(date(2026, 10, 1)))
        self.assertEqual(self.page.cal_list.count(), 1)
        self.assertIn("已投递", self.page.cal_list.item(0).text())

        self.page.cal_include_applied.setChecked(False)
        self.assertEqual(len(self.page._feed), EXPECT_ACTIONS)
        self.page.calendar.setSelectedDate(qdate(date(2026, 10, 1)))
        self.assertEqual(self.page.cal_list.count(), 0,
                         "取消勾选后旧记号/旧列表不能残留")

    def test_marks_are_cleared_not_accumulated(self):
        """`setDateTextFormat` 是累积的：不清旧记号就会出现「筛掉的颜色还在」。"""
        self.page.cal_include_applied.setChecked(True)
        self.page.cal_include_applied.setChecked(False)
        self.assertNotEqual(self.page.calendar.dateTextFormat(
            qdate(date(2026, 10, 1))).fontWeight(), QFont.Weight.Bold)

    def test_double_click_jumps_to_row(self):
        self.page.calendar.setSelectedDate(qdate(date(2026, 10, 11)))
        self.page.tabs.setCurrentIndex(1)
        self.page._on_calendar_item(self.page.cal_list.item(0))
        self.assertEqual(self.page.tabs.currentIndex(), 0, "应先切回列表页")
        self.assertEqual(self.page.table.currentRow(),
                         [a["id"] for a in APPS].index("a4"))


class TestExport(_TrackerCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="offflow_ics_")
        self.target = os.path.join(self.tmp, "求职日程.ics")

    def _patch_dialog(self, path):
        fake = mock.MagicMock()
        fake.getSaveFileName.return_value = (path, "日历文件 (*.ics)")
        p = mock.patch.object(tracker, "QFileDialog", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_export_writes_valid_readable_file(self):
        self._patch_dialog(self.target)
        self.page._export_ics()
        self.assertTrue(os.path.exists(self.target))
        with open(self.target, "rb") as fh:
            raw = fh.read()
        self.assertGreater(len(raw), 200)
        stripped = raw.replace(b"\r\n", b"")
        self.assertNotIn(b"\n", stripped, "必须全部 CRLF")
        self.assertNotIn(b"\r", stripped)
        self.assertLessEqual(max(len(l) for l in raw.split(b"\r\n")), 75)
        reread = ics.parse_calendar(raw.decode("utf-8"))
        self.assertEqual(len(reread), EXPECT_ACTIONS)
        self.assertEqual(sorted(e.get("SUMMARY") for e in reread),
                         sorted(ev.summary for ev in self.page._feed))
        # 落盘 = 由源码数据独立算出来的那份（证明 UI 没在中间做手脚）
        self.assertEqual(raw, calendar_feed.build_ics(APPS).encode("utf-8"))
        titles = [t for t, _ in self.messages("information")]
        self.assertIn("已导出", titles)

    def test_reverse_dirty_data_degrades_with_visible_notice(self):
        """脏数据 → 日历降级，但必须**说清楚**，不能刷成「无安排」。"""
        class _BadApp(dict):
            def get(self, *a, **k):
                raise RuntimeError("模拟脏数据")

        self.page._refresh_calendar(apps=[_BadApp()])
        self.assertEqual(self.page._feed, [])
        self.assertIn("失败", self.page.cal_day_label.text())
        self.assertNotIn("无安排", self.page.cal_day_label.text(),
                         "失败提示被后面的刷新覆盖掉了 —— 用户一个字都看不到")
        # 可恢复
        self.page._refresh()
        self.assertEqual(len(self.page._feed), EXPECT_ACTIONS)
        self.assertEqual(self.page._cal_error, "")

    def test_reverse_generation_failure_leaves_no_file(self):
        bad_path = os.path.join(self.tmp, "坏的.ics")
        self._patch_dialog(bad_path)
        with mock.patch.object(calendar_feed, "build_ics_from_feed",
                               side_effect=ValueError("模拟生成失败")):
            self.page._export_ics()
        self.assertFalse(os.path.exists(bad_path),
                         "生成失败却留下了半个文件")
        self.assertTrue(self.messages("warning"), "失败必须弹警告")

    def test_reverse_empty_calendar_is_refused(self):
        empty_path = os.path.join(self.tmp, "空的.ics")
        self._patch_dialog(empty_path)
        self.page._feed = []
        self.page._export_ics()
        self.assertFalse(os.path.exists(empty_path))
        titles = [t for t, _ in self.messages("information")]
        self.assertIn("没有可导出的日程", titles)
        # 对照补：同一条路径在有数据时确实写得出文件（不是路径本身不可写）
        self._patch_dialog(self.target)
        self.page._refresh()
        self.page._export_ics()
        self.assertTrue(os.path.exists(self.target))


class TestSingleDataSource(_TrackerCase):

    def test_refresh_reads_once_and_row_lookup_reuses_it(self):
        """行号只在**同一份列表**里才有意义。

        原实现里 `_refresh` 读一份填表格、`_selected_app` 又读一份再按行号取，
        两边顺序只要有一点不同（后台同步刚落地、并发删了一条），
        就会「选中的是张三、删掉的却是李四」。
        """
        calls = {"n": 0}

        def _counted():
            calls["n"] += 1
            return [dict(a) for a in APPS]

        with mock.patch.object(tracker.db, "list_applications", _counted):
            calls["n"] = 0
            self.page._refresh()
            self.assertEqual(calls["n"], 1,
                             "一次刷新应只读一次（表格与日历同源）")
            # ⚠ 必须先真的选中一行。不选的话 `currentRow()` 是 -1、
            # `_selected_app()` 直接 return None，这条断言就变成**空靶**
            # （变异测试抓到的：把实现改回「每次重读」，它照样绿）。
            self.page.table.selectRow(0)
            before = calls["n"]
            self.assertIsNotNone(self.page._selected_app(), "没选中行，判据是空靶")
            self.assertEqual(calls["n"], before, "按行号取记录不该再读一次库")

    def test_reverse_selected_app_uses_the_visible_list(self):
        self.page.table.selectRow(0)
        self.page._apps = [dict(a, company="被换掉") for a in APPS]
        self.assertIs(self.page._selected_app(), self.page._apps[0],
                      "取到的必须是表格那一份，而不是重新读库的结果")


if __name__ == "__main__":
    unittest.main(verbosity=2)
