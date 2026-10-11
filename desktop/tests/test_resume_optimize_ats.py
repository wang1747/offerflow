#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简历优化页「ATS 前后对比」的判据（G6）。

对齐网页版优化结果区顶部那三栏（优化前评分 / 优化后评分 / JD 关键词命中），
分四层验：

  1. **接口层**：``api_client._norm_ats`` 的契约 —— 尤其是「没有数据」与
     「0 分」必须分得开（照网页版 ``?? {score: 0}`` 补 0 就是把「没做」写成
     「考了 0 分」，属于伪造数据）；
  2. **几何层**：``charts.ScoreCompare`` 的**量程恒为 100** —— 这是它与
     ``BarChart``（量程 = 本组最大值）唯一但致命的差别，判据正反都写；
  3. **控件接线**：卡片显隐、差值文案与语义 class、两份结论列表、关键词
     三态标签、重跑/失败时清场；
  4. **产物级**：离屏 grab 后量两条形右端的位置 —— 量程写成「按最大值归一」
     时两条会一样长，这是**只有像素能证**的事（``ratios()`` 是同一份逻辑，
     逻辑错了它跟着错）。

反向对照见 `_mutate_ats_check.py`。

运行：python tests/test_resume_optimize_ats.py
"""

import os
import re
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QPoint                                     # noqa: E402
from PySide6.QtWidgets import (                                       # noqa: E402
    QApplication, QFrame, QMessageBox, QVBoxLayout, QWidget,
)

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import api_client as ac                        # noqa: E402
from offerflow_desktop.ui import charts, theme as app_theme           # noqa: E402
from offerflow_desktop.ui import tokens                               # noqa: E402
from offerflow_desktop.ui.pages import resume_optimize as ro           # noqa: E402

RESUMES = [{"id": "r1", "raw_text": "张三\nPython 后端\n教育：某大学",
            "parsed_json": {"position": "Python 后端"}, "version": 1}]
JDS = [{"id": "j1", "company": "合合信息", "position": "RAG 算法", "raw_text": "要求 Python"}]

# 服务端 /resume/optimize 返回里 ATS 那两项的形状（见
# backend/app/agents/tools/ats_checker.check_ats_compatibility 的返回值）。
ATS_BEFORE = {"score": 62, "issues": ["缺少手机号联系方式", "内容过短，可能信息不足"],
              "suggestions": [], "matched_keywords": ["Python"], "missing_keywords": ["RAG"]}
ATS_AFTER = {"score": 88, "issues": [],
             "suggestions": ["补充 JD 中的关键词：RAG"],
             "matched_keywords": ["Python", "RAG"], "missing_keywords": []}
GAP = {"matched": ["Python", "RAG"], "partial": ["SQL"], "missing": ["K8s"]}


class _FakeMW(QWidget):
    def refresh_status(self):
        pass

    def navigate(self, *a, **k):
        pass

    def open_agent(self, *a, **k):
        pass


# ============================================================
# 1. 接口层契约
# ============================================================

class TestAtsNormalize(unittest.TestCase):

    def test_missing_or_broken_payload_reads_as_none(self):
        """拿不到数据一律 None —— 页面据此隐藏整块，而不是显示 0 分。"""
        for bad in (None, {}, [], "x", 3, {"score": None}, {"score": "abc"},
                    {"score": 150}, {"score": -1}):
            with self.subTest(payload=bad):
                self.assertIsNone(ac._norm_ats(bad))

    def test_zero_score_is_real_data_not_missing(self):
        """0 分是**合法结果**（比如整份简历都是空），不能与「没有数据」混为一谈。"""
        out = ac._norm_ats({"score": 0})
        self.assertIsNotNone(out, "0 分被当成「没数据」→ 页面会把真实结论藏起来")
        self.assertEqual(out["score"], 0)

    def test_none_fields_become_empty_lists(self):
        """逐项的 None 必须变成 []，否则页面 for 一下就是 TypeError。"""
        out = ac._norm_ats({"score": 70, "issues": None, "suggestions": None})
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["suggestions"], [])
        self.assertEqual(out["matched_keywords"], [])
        self.assertEqual(out["missing_keywords"], [])

    def test_score_is_coerced_to_int_and_items_to_str(self):
        out = ac._norm_ats({"score": "70", "issues": [1, "缺邮箱"]})
        self.assertEqual(out["score"], 70)
        self.assertEqual(out["issues"], ["1", "缺邮箱"])


# ============================================================
# 2. 几何层：量程
# ============================================================

class TestScoreCompareGeometry(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _APP

    def _chart(self, before, after):
        chart = charts.ScoreCompare()
        chart.set_scores(before, after)
        return chart

    def test_scale_is_fixed_at_hundred_not_the_group_max(self):
        """`52 → 80`：两条比值必须是 0.52 / 0.80。

        ⚠ 数据**不能**选 (52, 100)：那组的最大值恰好等于满分，按「本组最大
        值」归一算出来也是 0.52/1.0，两种量程重合，这条判据就成了空靶
        （第一版就是这么写的，直到跑反向对照才发现）。
        """
        chart = self._chart(52, 80)
        self.assertAlmostEqual(chart.ratios()["优化前"], 0.52, places=4)
        self.assertAlmostEqual(chart.ratios()["优化后"], 0.80, places=4)
        self.assertAlmostEqual(chart.delta(), 28.0, places=4)

    def test_reverse_barchart_scale_would_flatten_the_same_data(self):
        """反面对照：同一份数据交给 BarChart，80 分那条会被拉成满格。

        这条划出两者的分水岭 —— 「量程」不是注释里的一句自说自话：
        80 分在等距刻度上该留出 20% 的空隙（离满分还差多少），
        按本组最大值归一则永远顶格（看着像考了满分）。
        """
        mine = self._chart(52, 80)
        self.assertAlmostEqual(mine.ratios()["优化前"], 0.52, places=4)
        self.assertAlmostEqual(mine.ratios()["优化后"], 0.80, places=4)

        bars = charts.BarChart()
        bars.set_items([("优化前", 52, "#f59e0b"), ("优化后", 80, "#16a34a")])
        self.assertAlmostEqual(bars.ratios()["优化前"], 0.65, places=4)
        self.assertAlmostEqual(bars.ratios()["优化后"], 1.0, places=4)

    def test_close_scores_still_differ(self):
        """`52 → 53` 只差 1 分，两条也必须长短可分（按最大值归一会糊成一样）。"""
        chart = self._chart(52, 53)
        r = chart.ratios()
        self.assertAlmostEqual(r["优化前"], 0.52, places=4)
        self.assertAlmostEqual(r["优化后"], 0.53, places=4)
        self.assertAlmostEqual(chart.delta(), 1.0, places=4)

    def test_scores_are_clamped_into_range(self):
        chart = self._chart(-10, 150)
        self.assertAlmostEqual(chart.ratios()["优化前"], 0.0, places=4)
        self.assertAlmostEqual(chart.ratios()["优化后"], 1.0, places=4)

    def test_half_filled_chart_is_not_data(self):
        """只给一条分数 → ``has_data()`` 必须是 False（两条不同尺的不能比）。

        ⚠ 这条**不能**靠页面那条 `_render_ats(x, None)` 来覆盖：页面层自己
        就有 `if not ats or not ats_after` 的守卫，`set_scores` 根本不会被
        调用，这里把 `all` 改成 `any` 也照样绿（反向对照 M6 抓到的空靶）。
        """
        self.assertFalse(self._chart(52, None).has_data())
        self.assertFalse(self._chart(None, 80).has_data())
        self.assertFalse(self._chart(None, None).has_data())
        self.assertTrue(self._chart(52, 80).has_data())

    def test_delta_sign_and_missing_propagation(self):
        self.assertEqual(self._chart(88, 62).delta(), -26.0)
        self.assertEqual(self._chart(70, 70).delta(), 0.0)
        # 缺失任一条 → None（**不是 0**；页面据此收起整块而不是显示「持平」）
        self.assertIsNone(self._chart(None, 80).delta())
        self.assertIsNone(self._chart(80, None).delta())
        self.assertIsNone(self._chart(None, None).delta())

    def test_empty_state_has_data_flag_and_keeps_a_height(self):
        chart = self._chart(None, None)
        self.assertFalse(chart.has_data())
        self.assertEqual(list(chart.ratios().values()), [0.0, 0.0])
        self.assertGreaterEqual(chart.sizeHint().height(), 52,
                                "空态也得有高度，否则整块塌成 0 高看不见")


# ============================================================
# 3. 页面接线
# ============================================================

class _Base(unittest.TestCase):

    def setUp(self):
        self.addCleanup(tokens.set_theme, "light")
        self.addCleanup(_APP.setStyleSheet, "")
        for target, value in (("list_resumes", list(RESUMES)),
                              ("list_jds", list(JDS))):
            p = mock.patch.object(ro.db, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        # QMessageBox 在 headless 下是模态 exec()，永久阻塞；换成不阻塞的替身
        self._qm = mock.MagicMock()
        patcher = mock.patch.object(ro, "QMessageBox", self._qm)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _page(self):
        page = ro.ResumeOptimizePage(_FakeMW())
        self.addCleanup(page.setParent, None)
        self.addCleanup(page.deleteLater)
        return page

    def _shown(self):
        """过了 `showEvent` 的页面：两个下拉已按库里的数据填好。

        本页构造期只搭界面（不起请求），下拉是 `showEvent` 里
        `QTimer.singleShot(0, self._refresh)` 才填的。凡是要走 `_optimize()`
        ——它读 `resume_combo.currentData()` —— 的用例都必须用这个入口。
        """
        page = self._page()
        page.show()
        _APP.processEvents()
        self.addCleanup(page.hide)
        return page


class TestAtsCardWiring(_Base):

    def test_constructor_keeps_the_card_hidden(self):
        """构造期不显形（也没有发请求）—— 空卡片挂在结果区最上面会误导。"""
        page = self._page()
        self.assertTrue(page.ats_card.isHidden())
        self.assertFalse(page.ats_chart.has_data())
        self.assertEqual(page.ats_keyword_host.count(), 0)

    def test_render_shows_card_with_scores_and_delta(self):
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        self.assertFalse(page.ats_card.isHidden())
        self.assertEqual(page.ats_chart.ratios(),
                         {"优化前": 0.62, "优化后": 0.88})
        self.assertEqual(page.ats_chart.delta(), 26.0)
        self.assertIn("提升 26", page.ats_delta_label.text())

    def test_reverse_render_without_ats_hides_the_card(self):
        """服务端没给 ATS → 整块收起，**绝不能**出现「0 分」。"""
        page = self._page()
        page._render_ats(None, None, GAP)
        self.assertTrue(page.ats_card.isHidden())
        self.assertEqual(page.ats_delta_label.text(), "")

    def test_reverse_half_missing_hides_the_card(self):
        """只给一半同样是「不可比」—— 两条不同尺的分数放一起是误导。"""
        page = self._page()
        page._render_ats(ATS_BEFORE, None, GAP)
        self.assertTrue(page.ats_card.isHidden())
        self.assertFalse(page.ats_chart.has_data())

    def test_delta_class_switches_with_sign(self):
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        self.assertEqual(page.ats_delta_label.property("class"), "text-success")

        page._render_ats(ATS_AFTER, ATS_BEFORE, GAP)
        self.assertEqual(page.ats_delta_label.property("class"), "text-danger")
        self.assertIn("下降 26", page.ats_delta_label.text())

        page._render_ats(ATS_BEFORE, ATS_BEFORE, GAP)
        self.assertEqual(page.ats_delta_label.property("class"), "hint")
        self.assertEqual(page.ats_delta_label.text(), "分数持平")

    def test_issues_and_suggestions_follow_the_payload(self):
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        # 布局第 0 项是列标题，所以条数 = 内容数 + 1
        self.assertEqual(page.ats_issues_layout.count(), len(ATS_BEFORE["issues"]) + 1)
        self.assertEqual(page.ats_suggestions_layout.count(),
                         len(ATS_AFTER["suggestions"]) + 1)
        self.assertIn("命中 2", page.ats_hit_label.text())
        self.assertIn("缺失 1", page.ats_hit_label.text())

    def test_empty_lists_show_a_placeholder_instead_of_a_gap(self):
        page = self._page()
        page._render_ats({"score": 90, "issues": [], "suggestions": []},
                         {"score": 95, "issues": [], "suggestions": []},
                         {"matched": [], "partial": [], "missing": []})
        self.assertEqual(page.ats_issues_layout.count(), 2)   # 标题 + 一句空态
        self.assertEqual(page.ats_suggestions_layout.count(), 2)
        self.assertEqual(page.ats_keyword_host.count(), 1)    # 一句「没有可对比的」

    def test_keyword_chips_grouped_by_status(self):
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        self.assertEqual(page.ats_keyword_host.count(),
                         len(GAP["matched"]) + len(GAP["partial"]) + len(GAP["missing"]))

    @staticmethod
    def _chip_colour(label) -> str:
        m = re.search(r"color:\s*(#[0-9a-fA-F]{6})", label.styleSheet())
        if not m:
            raise AssertionError("标签没有取到前景色：%r" % label.styleSheet())
        return m.group(1).lower()

    def test_keyword_chips_use_three_distinct_colours(self):
        """命中/对齐/缺失必须是**三种颜色** —— 全涂一个色等于没分类。"""
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        chips = page.ats_keyword_host.widgets()
        self.assertEqual(len(chips), 4)
        colours = [self._chip_colour(c) for c in chips]
        self.assertEqual(len(set(colours)), 3, "三种状态没有用三种颜色区分")
        self.assertEqual(colours[0], colours[1])        # 两条 matched 同色
        self.assertNotEqual(colours[1], colours[2])     # partial 换色
        self.assertNotEqual(colours[2], colours[3])     # missing 再换色

    def test_hit_tone_switches_with_the_theme(self):
        """三态色要跟着主题换档（深底上用浅色值会糊掉）。"""
        keys = ("matched", "partial", "missing")
        with mock.patch.object(ro.tokens, "is_dark", return_value=False):
            light = {k: ro._hit_tone(k) for k in keys}
        with mock.patch.object(ro.tokens, "is_dark", return_value=True):
            dark = {k: ro._hit_tone(k) for k in keys}
        for k in keys:
            self.assertNotEqual(light[k], dark[k], "%s 的配色没分主题" % k)
        self.assertEqual(len(set(light.values())), 3)
        self.assertEqual(len(set(dark.values())), 3)

    def test_clear_resets_everything(self):
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        page._clear_ats()
        self.assertTrue(page.ats_card.isHidden())
        self.assertFalse(page.ats_chart.has_data())
        self.assertEqual(page.ats_keyword_host.count(), 0)
        self.assertEqual(page.ats_delta_label.text(), "")
        # 列表要回到「只剩列标题」的初始态，而不是留着上一轮的几条结论
        self.assertEqual(page.ats_issues_layout.count(), 1)
        self.assertEqual(page.ats_suggestions_layout.count(), 1)

    def test_reverse_failed_optimize_clears_previous_result(self):
        """优化失败时上一轮的 ATS 必须清掉 —— 留在屏幕上会被当成这次结论。"""
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        self.assertFalse(page.ats_card.isHidden())
        page._on_failed("模拟接口炸了")
        self.assertTrue(page.ats_card.isHidden())
        self.assertEqual(page.ats_keyword_host.count(), 0)

    def test_rerun_clears_the_card_before_it_finishes(self):
        page = self._shown()                       # _optimize() 读下拉当前值
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        with mock.patch.object(ro, "LLMWorker") as worker, \
                mock.patch.object(ro.db, "get_resume",
                                  return_value=dict(RESUMES[0])), \
                mock.patch.object(ro.db, "get_jd", return_value=dict(JDS[0])):
            page._optimize()
        worker.return_value.start.assert_called_once()
        self.assertTrue(page.ats_card.isHidden(), "重跑期间还挂着上一轮的分数")


class TestAtsStatusLine(_Base):

    def _result(self, ats, ats_after):
        return {
            "optimized": "张三\nPython 后端工程师\n邮箱 a@b.com\n技能：Python、RAG",
            "optimized_text": "张三\nPython 后端工程师\n邮箱 a@b.com\n技能：Python、RAG",
            "changes": [], "added_keywords": ["RAG"], "removed": [],
            "length_warning": "", "gap": GAP, "edit_count": 3,
            "preservation": {"score": 1.0, "passed": True},
            "ats": ats, "ats_after": ats_after,
        }

    def _prepared(self):
        page = self._page()
        page._resume = {"id": "r1", "raw_text": "张三\nPython"}
        page._jd = {"id": "j1", "raw_text": "要求 Python RAG"}
        return page

    def test_status_line_reports_ats_when_available(self):
        page = self._prepared()
        page._on_optimized(self._result(ATS_BEFORE, ATS_AFTER))
        self.assertIn("ATS 62 → 88", page.status_label.text())
        self.assertFalse(page.ats_card.isHidden())

    def test_reverse_status_line_stays_silent_without_ats(self):
        """没有 ATS 数据时状态行不能凭空冒出「ATS 0 → 0」。"""
        page = self._prepared()
        page._on_optimized(self._result(None, None))
        self.assertNotIn("ATS", page.status_label.text())
        self.assertTrue(page.ats_card.isHidden())


# ============================================================
# 4. 产物级：两条形**真的**不等长
# ============================================================

class TestBarRendering(_Base):

    def _frame_with(self, chart):
        frame = QFrame()
        frame.setProperty("class", "card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(chart)
        frame.resize(400, 90)
        frame.show()
        _APP.processEvents()
        self.addCleanup(frame.hide)
        self.addCleanup(frame.setParent, None)
        return frame

    @staticmethod
    def _right_edge(img, y, x0, x1):
        """从右往左找第一个明显偏离背景的像素 = 条的右端。"""
        bg = img.pixelColor(1, 1)
        for x in range(x1, x0 - 1, -1):
            c = img.pixelColor(x, y)
            if (abs(c.red() - bg.red()) > 40 or abs(c.green() - bg.green()) > 40
                    or abs(c.blue() - bg.blue()) > 40):
                return x
        return None

    def test_two_bars_render_at_proportionally_different_lengths(self):
        """52 与 80 两条的右端，既要有比例差、又都不能顶格。

        ⚠ 这条**故意**是产物级而非逻辑级：``ratios()`` 与绘制用的是同一个
        ``FULL`` 常量，逻辑层把量程写错时两边一起错、逻辑判据跟着一起绿。
        只有量像素才能证明「画出来真的是两根按 0~100 定位的条」。

        用 (52, 80) 而不是 (52, 100)：后者满格那条两种量程下都一样，
        区分不出来（同上一条注释里的空靶）。
        """
        _APP.setStyleSheet(app_theme.MAIN_QSS)
        tokens.set_theme("light")
        chart = charts.ScoreCompare()
        chart.set_scores(52, 80)
        frame = self._frame_with(chart)
        _APP.processEvents()

        origin = chart.mapTo(frame, QPoint(0, 0))
        img = frame.grab().toImage()
        # 两行条的中心 y：ROW_H/2 与 ROW_H+ROW_GAP+ROW_H/2
        y1 = origin.y() + chart.ROW_H // 2
        y2 = origin.y() + chart.ROW_H + chart.ROW_GAP + chart.ROW_H // 2
        x0 = origin.x() + chart.LABEL_W
        x1 = origin.x() + chart.width() - chart.VALUE_W - 2

        r1 = self._right_edge(img, y1, x0, x1)
        r2 = self._right_edge(img, y2, x0, x1)
        self.assertIsNotNone(r1, "第一条完全没画出来")
        self.assertIsNotNone(r2, "第二条完全没画出来")

        span = x1 - x0
        self.assertAlmostEqual((r2 - r1) / span, 0.28, delta=0.10,
                               msg="两条条形长度几乎一样")
        # 80 分的条**不该**顶到最右 —— 顶格说明量程被当成了「本组最大值」
        self.assertLess(r2, x1 - span * 0.10,
                        "80 分那条顶到了满格 —— 量程写成了按最大值归一")

    def test_card_has_no_page_background_leak(self):
        """卡片里不该出现**页面底色**（两套主题各自验一遍）。

        全局 QSS 有一条 `QMainWindow, QWidget { background: ... }`，任何
        没声明透明的布局容器（QWidget / FlowWidget）在卡片里都会刷出一块
        页面底色 —— 截图里一眼能看出脏，但功能判据全绿。

        判据用「面积占比」而不是点采样：文字与条形会盖掉一部分像素，
        点采样容易碰到字。阈值 5% 对应的是「只有卡片四角是页面底色」。
        """
        page = self._page()
        page._render_ats(ATS_BEFORE, ATS_AFTER, GAP)
        page.show()
        self.addCleanup(page.hide)

        for qss, tone, page_bg in (
                (app_theme.MAIN_QSS, "light", (0xf7, 0xf8, 0xfa)),
                (app_theme.DARK_QSS, "dark", (0x17, 0x17, 0x1c))):
            _APP.setStyleSheet(qss)
            tokens.set_theme(tone)
            _APP.processEvents()
            img = page.ats_card.grab().toImage()
            hits = 0
            for y in range(0, img.height(), 2):
                for x in range(0, img.width(), 2):
                    c = img.pixelColor(x, y)
                    if (abs(c.red() - page_bg[0]) <= 1 and
                            abs(c.green() - page_bg[1]) <= 1 and
                            abs(c.blue() - page_bg[2]) <= 1):
                        hits += 1
            total = (img.height() // 2) * (img.width() // 2)
            ratio = hits / float(total)
            self.assertLess(
                ratio, 0.05,
                "%s 主题下卡片里出现了成片的页面底色（%.1f%%）—— "
                "布局容器没声明透明" % (tone, ratio * 100))

    def test_reverse_empty_chart_paints_no_bar_colours(self):
        """空态下不该出现条形色（否则「没有数据」也会画出一根满格条）。"""
        _APP.setStyleSheet(app_theme.MAIN_QSS)
        tokens.set_theme("light")
        chart = charts.ScoreCompare()
        chart.set_scores(None, None)
        frame = self._frame_with(chart)
        _APP.processEvents()

        img = frame.grab().toImage()
        bg = img.pixelColor(1, 1)
        hits = 0
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if (abs(c.red() - bg.red()) > 40 or abs(c.green() - bg.green()) > 40
                        or abs(c.blue() - bg.blue()) > 40):
                    hits += 1
        # 空态只画一圈虚线 + 一行字，不该有大片实心色块
        self.assertLess(hits, img.width() * img.height() * 0.25,
                        "空态画布上有大片实色，像是把条也画了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
