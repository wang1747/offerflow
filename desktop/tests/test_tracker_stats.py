#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递追踪「统计视图」的 UI 层判据（G5）。

判据分三层：
  1. **几何层**（不起窗口）：条长 ∝ 数值、扇区张角 ∝ 数值 —— 是数值判据，
     不用对着像素猜；
  2. **控件状态**：四张卡片的数字、漏斗/环图的条目与顺序、口径提示、
     空态与失败态、以及「统计不跟着状态筛选走」；
  3. **产物级**：离屏 grab 后图上**真的出现**当前主题的辅助色，
     且换主题时会跟着变 —— 手绘控件的取色写错是**静默**的
     （QSS 里选择器写错顶多不生效，QPainter 里写错就是画错颜色），
     所以「源码里有这行」不算数。

反向对照见 `_mutate_stats_check.py`。

运行：python tests/test_tracker_stats.py
"""

import copy
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEvent                                    # noqa: E402
from PySide6.QtWidgets import QApplication, QFrame, QWidget          # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.config import (                                # noqa: E402
    STATUS_COLORS, STATUS_COLORS_DARK,
)
from offerflow_desktop.ui import charts, theme as app_theme           # noqa: E402
from offerflow_desktop.ui import tokens                               # noqa: E402
from offerflow_desktop.ui.pages import tracker                        # noqa: E402

APPS = [
    {"id": "a1", "company": "腾讯", "position": "AI 应用研发",
     "status": "first_interview", "applied_date": "2026-10-01",
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-01T10:00:00"},
    {"id": "a2", "company": "合合信息", "position": "RAG 算法",
     "status": "applied", "applied_date": "2026-10-02",
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-02T10:00:00"},
]

# 服务端 /applications/stats 的形状（与 backend services.get_statistics 一致）。
# 各状态计数之和 == total，这是服务端保证的（total = len(apps)）。
STATS = {
    "total": 7,
    "status_order": ["saved", "applied", "online_test", "first_interview",
                     "second_interview", "hr_round", "offered", "accepted",
                     "rejected"],
    "status_counts": {"saved": 2, "applied": 1, "online_test": 1,
                      "first_interview": 1, "second_interview": 0,
                      "hr_round": 0, "offered": 0, "accepted": 1, "rejected": 1},
    "conversion_rate": {"applied_to_interview": 33.3, "interview_to_offer": 0.0},
}

# 漏斗只含「一条投递会依次经过的状态」：saved/applied/online_test/first_interview
# （后三个阶段计数为 0，被过滤）。终局分支 accepted/rejected 不在漏斗里。
EXPECT_FUNNEL = ["已收藏", "已投递", "笔试", "一面"]
# 状态分布走 status_order，含终局状态（0 值过滤掉）
EXPECT_DIST = ["已收藏", "已投递", "笔试", "一面", "已接受", "已挂"]


class _FakeMW(QWidget):
    def refresh_status(self):
        pass

    def navigate(self, *a, **k):
        pass

    def open_agent(self, *a, **k):
        pass


def _rgb(hexstr):
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _image_colors(img):
    """整张图的像素颜色集合（含背景）。"""
    out = set()
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            out.add((c.red(), c.green(), c.blue()))
    return out


def _has_near(colors, target, tol=26):
    tr, tg, tb = _rgb(target)
    for r, g, b in colors:
        if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
            return True
    return False


# 空态辅助色：浅色取 tokens.C.BORDER（**引用常量，不抄字面量** ——
# 抄错就成空靶：这次抄成 QSS 里列表槽边框的 #e2e8f0，与真值 #e8eaf0
# 只差 6，落在容差内，看着还是绿的）；深色在 charts._palette() 里字面写死。
#
# ⚠ 判据必须**双向**：只断言「深色图里能找到 #3a3a46」是空靶 ——
# 深色卡片底 #26262f=(38,38,47) 与 #3a3a46=(58,58,70) 只差 20/20/23，
# 落在容差内，于是「取色没分档」这种缺陷照样过（变异 M9 抓到的）。
LIGHT_BORDER = tokens.C.BORDER
DARK_BORDER = "#3a3a46"


class _Base(unittest.TestCase):
    """公共装置：打桩投递列表与统计接口，并记录统计被请求的次数。"""

    def setUp(self):
        self.calls = {"stats": 0}

        def _stats():
            self.calls["stats"] += 1
            return copy.deepcopy(STATS)

        self._patches = [
            mock.patch.object(tracker.db, "list_applications",
                              side_effect=lambda: [dict(a) for a in APPS]),
            mock.patch.object(tracker.db, "get_statistics", side_effect=_stats),
            mock.patch.object(tracker, "QMessageBox"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.mw = _FakeMW()
        self.page = tracker.TrackerPage(self.mw)
        self.addCleanup(self.page.hide)
        self.addCleanup(self.page.setParent, None)

    def show(self):
        """真显示一次（showEvent 是统计的加载入口，不显示就永远不加载）。"""
        self.page.show()
        _APP.processEvents()


# ============================================================
# 1. 几何层：不依赖绘制
# ============================================================

class TestBarChartGeometry(unittest.TestCase):

    def setUp(self):
        self.chart = charts.BarChart()

    def test_bar_length_is_proportional_to_value(self):
        self.chart.set_items([("甲", 4, "#111111"), ("乙", 2, "#222222"),
                              ("丙", 1, "#333333")])
        self.assertEqual(self.chart.ratios(), {"甲": 1.0, "乙": 0.5, "丙": 0.25})

    def test_zero_valued_rows_are_dropped(self):
        self.chart.set_items([("甲", 2, "#111111"), ("乙", 0, "#222222")])
        self.assertEqual([lab for lab, _, _ in self.chart.visible_items()], ["甲"])
        self.assertNotIn("乙", self.chart.ratios())

    def test_reverse_all_zero_does_not_crash_and_has_no_bars(self):
        """全 0 时最大值也是 0 —— 直接拿它做分母就是 ZeroDivisionError。"""
        self.chart.set_items([("甲", 0, "#111111"), ("乙", 0, "#222222")])
        self.assertEqual(self.chart.visible_items(), [])
        self.assertEqual(self.chart.ratios(), {})
        self.assertGreater(self.chart.height(), 0)

    def test_height_grows_with_row_count(self):
        self.chart.set_items([("甲", 1, "#111111")])
        one = self.chart.sizeHint().height()
        self.chart.set_items([("甲", 1, "#111111"), ("乙", 1, "#222222"),
                              ("丙", 1, "#333333")])
        self.assertGreater(self.chart.sizeHint().height(), one,
                           "行数变多高度不变 ⇒ 布局会把行压在一起")


class TestDonutGeometry(unittest.TestCase):

    def setUp(self):
        self.chart = charts.DonutChart()

    def test_span_is_proportional_and_sums_to_full_circle(self):
        self.chart.set_items([("甲", 3, "#111111"), ("乙", 1, "#222222")])
        geom = self.chart.slice_geometry()
        self.assertEqual([g[0] for g in geom], ["甲", "乙"])
        self.assertAlmostEqual(geom[0][2], 270.0, places=4)
        self.assertAlmostEqual(geom[1][2], 90.0, places=4)
        self.assertAlmostEqual(sum(g[2] for g in geom), 360.0, places=4)

    def test_starts_at_twelve_oclock_and_goes_clockwise(self):
        self.chart.set_items([("甲", 1, "#111111"), ("乙", 1, "#222222")])
        geom = self.chart.slice_geometry()
        self.assertAlmostEqual(geom[0][1], 90.0, places=4)
        # 顺时针推进 = 角度递减；-90° 与 270° 是同一个方向，取模后再比
        self.assertAlmostEqual(geom[1][1] % 360.0, 270.0, places=4)
        self.assertLess(geom[1][1], geom[0][1], "没有顺时针推进")

    def test_single_slice_is_a_full_circle(self):
        self.chart.set_items([("甲", 5, "#111111")])
        self.assertAlmostEqual(self.chart.slice_geometry()[0][2], 360.0, places=4)

    def test_reverse_empty_has_no_geometry(self):
        self.assertEqual(self.chart.slice_geometry(), [])
        self.chart.set_items([("甲", 0, "#111111")])
        self.assertEqual(self.chart.slice_geometry(), [],
                         "0 值也画 ⇒ 画出一个没数据的扇区")


# ============================================================
# 2. 控件状态
# ============================================================

class TestStatsTab(_Base):

    def test_tab_exists(self):
        self.assertEqual([self.page.tabs.tabText(i)
                          for i in range(self.page.tabs.count())],
                         ["看板与列表", "日历", "统计"])

    def test_cards_show_server_numbers(self):
        self.show()
        self.assertEqual(self.page._stats_cards["total"].value_label.text(), "7")
        self.assertEqual(
            self.page._stats_cards["applied_to_interview"].value_label.text(), "33.3%")
        self.assertEqual(
            self.page._stats_cards["interview_to_offer"].value_label.text(), "0%")
        self.assertEqual(self.page._stats_cards["accepted"].value_label.text(), "1")

    def test_funnel_keeps_stage_order_and_drops_zeroes(self):
        self.show()
        chart = self.page.funnel_chart
        # 传入的是**七个阶段全量**（顺序固定），过滤发生在渲染层
        self.assertEqual([lab for lab, _, _ in chart.items()],
                         ["已收藏", "已投递", "笔试", "一面", "二面", "HR 面", "已发 Offer"])
        self.assertEqual([lab for lab, _, _ in chart.visible_items()], EXPECT_FUNNEL)
        self.assertEqual([v for _, v, _ in chart.visible_items()], [2, 1, 1, 1])
        self.assertTrue(all(v > 0 for _, v, _ in chart.visible_items()),
                        "0 值阶段混进了漏斗")

    def test_distribution_uses_status_order_including_terminal_states(self):
        """分布要含 accepted/rejected（终局），漏斗不含 —— 两者不是一套。

        顺序必须来自服务端的 `status_order`，不是漏斗那套固定列表：
        否则「已接受/已挂」会掉到末尾或干脆消失。
        """
        self.show()
        chart = self.page.status_chart
        self.assertEqual([lab for lab, _, _ in chart.items()],
                         ["已收藏", "已投递", "笔试", "一面", "二面", "HR 面",
                          "已发 Offer", "已接受", "已挂"])
        self.assertEqual([lab for lab, _, _ in chart.visible_items()], EXPECT_DIST)
        # 六个非零状态、计数之和 7（已收藏 2 + 其余五个各 1），与 total 一致
        self.assertEqual(chart.total(), 7.0)

    def test_accepts_count_parity_between_funnel_and_real_counts(self):
        """漏斗不吞数据：分布里的总数 == 服务端非零计数之和。"""
        self.show()
        shown = {lab: v for lab, v, _ in self.page.status_chart.visible_items()}
        non_zero = sum(v for v in STATS["status_counts"].values() if v > 0)
        self.assertEqual(sum(shown.values()), non_zero)

    def test_scope_note_says_it_ignores_the_filter(self):
        self.show()
        self.assertIn("全部投递", self.page.stats_scope.text())

    def test_stats_do_not_follow_the_status_filter(self):
        """按状态筛选后统计口径不变。

        直接拿 `self._apps` 算会得到「筛过的数字」—— 筛选「已挂」时
        「投递总数」变成 1，那是错的（统计口径是全量）。
        """
        self.show()
        self.page.filter_combo.setCurrentIndex(1)     # 任意一个具体状态
        self.page._load_stats()
        self.assertEqual(self.page._stats_cards["total"].value_label.text(), "7")
        self.assertEqual(len(self.page.funnel_chart.visible_items()),
                         len(EXPECT_FUNNEL))

    def test_empty_stats_render_empty_states_without_crash(self):
        self.show()
        with mock.patch.object(tracker.db, "get_statistics",
                               return_value={"total": 0, "status_counts": {},
                                             "status_order": []}):
            self.page._load_stats()
        self.assertEqual(self.page._stats_cards["total"].value_label.text(), "0")
        self.assertEqual(self.page.funnel_chart.visible_items(), [])
        self.assertEqual(self.page.status_chart.slice_geometry(), [])

    def test_reverse_failure_degrades_and_keeps_the_board_alive(self):
        self.show()
        with mock.patch.object(tracker.db, "get_statistics",
                               side_effect=RuntimeError("模拟统计接口炸了")):
            self.page._load_stats()
        self.assertIn("失败", self.page.stats_scope.text())
        self.assertEqual(self.page.funnel_chart.visible_items(), [])
        # 统计是附加视图：它坏了，看板/列表/日历必须还好用
        self.assertEqual(self.page.table.rowCount(), len(APPS))
        self.assertEqual(len(self.page._columns), 9)
        # 恢复：不在统计页时 `_refresh` 只标脏，切过去才重载（不重复打请求），
        # 所以这里走真实链路 —— 切回统计 Tab
        self.page.tabs.setCurrentIndex(0)
        self.page._refresh()
        self.page.tabs.setCurrentIndex(2)
        self.assertEqual(self.page.stats_scope.text().count("失败"), 0,
                         "恢复后还挂着失败提示")
        self.assertEqual(self.page._stats_cards["total"].value_label.text(), "7")

    def test_theme_decides_which_status_palette_is_used(self):
        self.show()
        with mock.patch.object(tracker.tokens, "is_dark", return_value=False):
            self.page._render_stats()
            light = [c for _, _, c in self.page.funnel_chart.items()]
        with mock.patch.object(tracker.tokens, "is_dark", return_value=True):
            self.page._render_stats()
            dark = [c for _, _, c in self.page.funnel_chart.items()]
        self.assertEqual(light[0], STATUS_COLORS["saved"])
        self.assertEqual(dark[0], STATUS_COLORS_DARK["saved"])
        self.assertNotEqual(light, dark, "深浅两套状态色必须真的不同")


# ============================================================
# 3. 加载时机（懒加载：没人看就不打这个网络请求）
# ============================================================

class TestStatsLoadTiming(_Base):

    def test_ctor_does_not_fetch_stats(self):
        """构造只搭界面。在 `__init__` 里发请求，重建窗口时会卡住。"""
        self.assertEqual(self.calls["stats"], 0)
        self.assertEqual(self.page._stats_cards["total"].value_label.text(), "—")

    def test_first_show_fetches_once(self):
        self.show()
        self.assertEqual(self.calls["stats"], 1)
        self.page.hide()
        self.page.show()
        _APP.processEvents()
        self.assertEqual(self.calls["stats"], 1, "不脏就不该重复打请求")

    def test_refresh_off_tab_only_marks_dirty(self):
        self.show()
        base = self.calls["stats"]
        self.page.tabs.setCurrentIndex(0)
        self.page._refresh()
        self.assertEqual(self.calls["stats"], base)
        self.assertTrue(self.page._stats_dirty)
        self.page.tabs.setCurrentIndex(2)          # 切过去才补
        self.assertEqual(self.calls["stats"], base + 1)

    def test_refresh_on_tab_reloads_immediately(self):
        self.show()
        self.page.tabs.setCurrentIndex(2)
        base = self.calls["stats"]
        self.page._refresh()
        self.assertEqual(self.calls["stats"], base + 1,
                         "正停在统计页，改完却要用户手动刷新")

    def test_refresh_button_reloads(self):
        self.show()
        base = self.calls["stats"]
        self.page.stats_refresh_btn.click()
        self.assertEqual(self.calls["stats"], base + 1)


# ============================================================
# 4. 产物级：手绘画布上的颜色真的跟着主题走
# ============================================================

class TestChartRendering(_Base):

    def setUp(self):
        super().setUp()
        # 这两个用例会把进程级主题状态切来切去，跑完必须归位 ——
        # 否则后面的用例会莫名其妙拿到深色档（用例之间不该有隐式依赖）。
        self.addCleanup(tokens.set_theme, "light")
        self.addCleanup(_APP.setStyleSheet, "")

    def _frame_with(self, widget):
        frame = QFrame()
        frame.setProperty("class", "card")
        from PySide6.QtWidgets import QVBoxLayout
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(widget)
        frame.resize(200, 90)
        frame.show()
        _APP.processEvents()
        self.addCleanup(frame.hide)
        self.addCleanup(frame.setParent, None)
        return frame

    def _bg_color(self, img):
        c = img.pixelColor(1, 1)
        return (c.red(), c.green(), c.blue())

    def test_palette_switches_with_theme(self):
        """辅助色必须分档 —— 逻辑判据：直接看取色函数。

        这条故意不放产物级：颜色最终由 `_palette()` 决定，直接断言它
        比对着像素猜可靠得多（像素那层有反锯齿干扰，见下面那条注释）。
        """
        with mock.patch.object(charts.tokens, "is_dark", return_value=False):
            light = charts._palette()
        with mock.patch.object(charts.tokens, "is_dark", return_value=True):
            dark = charts._palette()
        self.assertEqual(light["empty_border"], LIGHT_BORDER)
        self.assertEqual(dark["empty_border"], DARK_BORDER)
        self.assertNotEqual(light["text"], dark["text"])
        self.assertNotEqual(light["track"], dark["track"])
        self.assertNotEqual(light["sub"], dark["sub"])

    def test_reverse_rendered_canvas_never_shows_the_other_theme_border(self):
        """产物级**否命题**：浅色画布上不该出现深色边框，反之亦然。

        ⚠ 正命题（「一定能在图上找到本主题的边框色」）**故意不写**：
        空态那圈虚线只有 0.5px，反锯齿后没有纯色像素，只能靠"与背景的
        混合色"去凑 —— 而浅色背景 #f7f8fa 与浅色边框 #e8eaf0 只差
        15/14/10、深色卡片底 #26262f 与深色边框 #3a3a46 只差 20/20/23，
        随便哪个近似色就能顶替它。这种正命题是空靶（变异 M9/M10 连着
        抓出来两次）。否命题用**宽**容差，越宽越严格，抓的正是
        「整档没切」这种真实缺陷。
        """
        chart = charts.BarChart("还没有投递记录")
        frame = self._frame_with(chart)

        for qss, name, other in (
                (app_theme.MAIN_QSS, "light", DARK_BORDER),
                (app_theme.DARK_QSS, "dark", LIGHT_BORDER)):
            _APP.setStyleSheet(qss)
            tokens.set_theme(name)
            chart.update()
            _APP.processEvents()
            img = frame.grab().toImage()
            others = _image_colors(img) - {self._bg_color(img)}
            self.assertFalse(_has_near(others, other),
                             "%s 主题的画布上出现了 %s 的边框色 —— 取色没分档"
                             % (name, "深色" if name == "light" else "浅色"))

    def test_reverse_repaint_is_triggered_by_style_change(self):
        """StyleChange 必须触发 update()（行为判据）。

        为什么重绘这件事只能用行为判据证：见上一条注释（0.5px 虚线的
        像素判据是空靶）。行为层能明确回答「事件有没有被接住」，
        而「颜色对不对」由 `test_palette_switches_with_theme` 负责，
        两者合起来才是完整的。
        """
        chart = charts.DonutChart("还没有投递记录")
        with mock.patch.object(chart, "update") as up:
            chart.changeEvent(QEvent(QEvent.Type.StyleChange))
        up.assert_called_once()
        # 反向：别的事件不该引起重绘（否则等于"任何事件都全量重画"）
        with mock.patch.object(chart, "update") as up:
            chart.changeEvent(QEvent(QEvent.Type.Show))
        up.assert_not_called()

    def test_reverse_theme_switch_repaints_existing_canvas(self):
        """完整切主题（QSS + token）后，已存在的画布整体跟着换档。

        走真实链路：`setStyleSheet` + `set_theme`（`apply_theme` 的顺序），
        **中间不手动 update()**。
        """
        chart = charts.DonutChart("还没有投递记录")
        frame = self._frame_with(chart)

        _APP.setStyleSheet(app_theme.MAIN_QSS)
        tokens.set_theme("light")
        chart.update()
        _APP.processEvents()
        before = self._bg_color(frame.grab().toImage())

        _APP.setStyleSheet(app_theme.DARK_QSS)
        tokens.set_theme("dark")
        _APP.processEvents()
        after = self._bg_color(frame.grab().toImage())
        self.assertNotEqual(before, after, "切主题后画布没变")

        img = frame.grab().toImage()
        others = _image_colors(img) - {self._bg_color(img)}
        self.assertFalse(_has_near(others, LIGHT_BORDER),
                         "切到深色后仍画着浅色边框")


if __name__ == "__main__":
    unittest.main(verbosity=2)
