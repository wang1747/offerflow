#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本页「调用链下钻」的判据（G9）。

三层：
  1. **纯函数层**：`span_rows` 的单元格口径（费用未知不能显示成 ¥0.00）；
  2. **几何层**：瀑布图的条位置/长度 ∝ 时间 —— `bars()` 给出 (左占比, 宽占比)，
     直接断言数值，不用对着像素猜；重点是**左偏移真的参与了计算**
     （只算宽度的话「谁先谁后」就丢了，这正是瀑布图存在的意义）；
  3. **控件层**：点行才拉明细、手风琴只开一条、再点收起、失败有说明。
     trace 详情是**点开时才请求**的，所以还额外盯「构造期不发请求」。

反向对照见 `_mutate_p1_check.py`。

运行：python tests/test_cost_trace_detail.py [TestCase.test_method]
"""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtGui import QShowEvent                                # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget                  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.api_client import ApiError                    # noqa: E402
from offerflow_desktop.ui import charts                              # noqa: E402
from offerflow_desktop.ui.pages import cost                          # noqa: E402

TRACES = [
    {"id": "t1", "operation": "resume_generate", "status": "success",
     "total_tokens": 1500, "total_cost": 0.01, "total_cost_cny": 0.07,
     "duration_ms": 900, "created_at": "2026-10-09T02:00:00.000000"},
    {"id": "t2", "operation": "jd_parse", "status": "failed",
     "total_tokens": 300, "total_cost": 0.0, "total_cost_cny": 0.0,
     "duration_ms": 120, "created_at": "2026-10-09T03:00:00.000000"},
]

SUMMARY = {"total_cost": 0.01, "total_cost_cny": 0.07, "total_tokens": 1800,
           "trace_count": 2, "by_operation": [], "by_model": []}

#: 第 2 条故意给它 failed + cost 未知（后端查不到该模型价格时给 null）
SPANS = [
    {"id": "s1", "model": "deepseek-chat", "prompt_tokens": 1200,
     "completion_tokens": 300, "total_tokens": 1500, "cost_cny": 0.0123,
     "duration_ms": 800, "start_offset_ms": 0, "status": "success"},
    {"id": "s2", "model": "qwen-plus", "prompt_tokens": 900,
     "completion_tokens": 0, "total_tokens": 900, "cost_cny": None,
     "duration_ms": 400, "start_offset_ms": 300, "status": "error"},
]

DETAIL = {"trace": {"id": "t1", "operation": "resume_generate",
                    "duration_ms": 900, "span_count": 2},
          "spans": SPANS}


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _SyncWorker:
    """同步假 worker：立即执行，不发线程、不打真实接口。"""

    def __init__(self, fn):
        self._fn = fn
        self.success = _Sig()
        self.failed = _Sig()

    def start(self):
        try:
            self.success.emit(self._fn())
        except Exception as exc:          # noqa: BLE001 —— 对齐 LLMWorker.run
            self.failed.emit(str(exc))


class _FakeMW(QWidget):
    def navigate(self, *args, **kwargs):
        pass

    def refresh_status(self):
        pass

    def open_agent(self, *args, **kwargs):
        pass


# ======================== 1. 纯函数层 ========================

class TestSpanRows(unittest.TestCase):
    def test_rows_keep_span_order_and_format_cells(self):
        rows = cost.span_rows(DETAIL)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "deepseek-chat")
        self.assertEqual(rows[0][1], "1,200")
        self.assertEqual(rows[0][2], "300")
        self.assertEqual(rows[0][3], "800ms")
        self.assertEqual(rows[1][0], "qwen-plus")

    def test_unknown_cost_says_unknown_not_zero(self):
        # 显示 ¥0.00 会让用户以为这次调用免费（实际是后端查不到价格）
        rows = cost.span_rows(DETAIL)
        self.assertEqual(rows[1][4], "未知")
        self.assertNotIn("0.00", rows[1][4])

    def test_empty_payload_yields_no_rows(self):
        self.assertEqual(cost.span_rows({}), [])
        self.assertEqual(cost.span_rows({"spans": [None, 3]}), [])

    def test_missing_model_falls_back_to_placeholder(self):
        rows = cost.span_rows({"spans": [{"id": "x", "cost_cny": 0.0}]})
        self.assertEqual(rows[0][0], "未知")


# ======================== 2. 几何层 ========================

class TestWaterfallGeometry(unittest.TestCase):
    def _chart(self, spans=None, total=0):
        c = charts.WaterfallChart()
        self.addCleanup(c.deleteLater)
        c.set_spans(spans if spans is not None else SPANS, total)
        return c

    def test_total_is_max_of_overall_and_span_ends(self):
        # 整体耗时比 span 结束时刻短时，取 span 结束时刻（否则有条会画到框外）
        # SPANS 的结束时刻：0+800=800、300+400=700 → 取 800
        c = self._chart(SPANS, total=100)
        self.assertEqual(c.total_ms(), 800)
        c2 = self._chart(SPANS, total=900)
        self.assertEqual(c2.total_ms(), 900)

    def test_total_never_zero(self):
        c = self._chart([], total=0)
        self.assertEqual(c.total_ms(), 1.0)

    def test_left_offset_really_participates(self):
        bars = self._chart(SPANS, total=900).bars()
        self.assertAlmostEqual(bars[0][1], 0.0, places=6)
        self.assertAlmostEqual(bars[1][1], 300 / 900, places=6)

    def test_width_is_duration_over_total(self):
        bars = self._chart(SPANS, total=900).bars()
        self.assertAlmostEqual(bars[0][2], 800 / 900, places=6)
        self.assertAlmostEqual(bars[1][2], 400 / 900, places=6)

    def test_zero_duration_span_still_visible(self):
        c = self._chart([{"id": "z", "model": "m", "duration_ms": 0,
                          "start_offset_ms": 0, "status": "success"}], total=1000)
        self.assertEqual(c.bars()[0][2], charts.WaterfallChart.MIN_WIDTH_RATIO)

    def test_failed_flag(self):
        bars = self._chart(SPANS, total=900).bars()
        self.assertFalse(bars[0][3])
        self.assertTrue(bars[1][3])

    def test_is_failed_tolerates_case_and_padding(self):
        for raw in ("error", "failed", "ERROR", " Failed ", "failed "):
            self.assertTrue(charts.WaterfallChart.is_failed(raw), raw)
        for raw in ("success", "", None, "ok"):
            self.assertFalse(charts.WaterfallChart.is_failed(raw), repr(raw))

    def test_failed_bar_uses_failure_color(self):
        c = self._chart(SPANS, total=900)
        self.assertEqual(c.bar_color(0, False), charts.WaterfallChart.OK_COLOR_A)
        self.assertEqual(c.bar_color(1, False), charts.WaterfallChart.OK_COLOR_B)
        self.assertEqual(c.bar_color(0, True), charts.WaterfallChart.FAIL_COLOR)

    def test_empty_state_hint_size_is_not_zero(self):
        c = self._chart([], total=0)
        self.assertGreater(c.sizeHint().height(), 0)


# ======================== 3. 控件层 ========================

class TestTraceRow(unittest.TestCase):
    def _row(self):
        r = cost.TraceRow(TRACES[0])
        self.addCleanup(r.deleteLater)
        return r

    def test_closed_by_default(self):
        r = self._row()
        self.assertFalse(r.is_open)
        self.assertTrue(r.detail.isHidden())
        self.assertIsNone(r.waterfall)

    def test_toggle_emits_trace_id(self):
        r = self._row()
        seen = []
        r.toggled.connect(seen.append)
        r.toggle()
        self.assertEqual(seen, ["t1"])

    def test_set_open_switches_visual_state(self):
        r = self._row()
        r.set_open(True)
        self.assertTrue(r.is_open)
        self.assertFalse(r.detail.isHidden())
        r.set_open(False)
        self.assertTrue(r.detail.isHidden())

    def test_set_detail_builds_waterfall_and_span_table(self):
        r = self._row()
        r.set_detail(DETAIL)
        self.assertIsNotNone(r.waterfall)
        self.assertEqual(r.waterfall.spans(), SPANS)
        self.assertEqual(r.span_table.rowCount(), len(SPANS))
        self.assertEqual(r.span_table.columnCount(), 5)
        self.assertEqual(r.span_table.item(1, 4).text(), "未知")

    def test_set_detail_without_spans_shows_placeholder(self):
        r = self._row()
        r.set_detail({"trace": {}, "spans": []})
        self.assertIsNone(r.waterfall)
        self.assertEqual(r.detail_layout.count(), 1)

    def test_set_message_clears_previous_detail(self):
        r = self._row()
        r.set_detail(DETAIL)
        r.set_message("读取明细中…")
        self.assertIsNone(r.waterfall)
        self.assertIsNone(r.span_table)
        self.assertEqual(r.detail_layout.count(), 1)

    def test_clear_detail_removes_everything(self):
        r = self._row()
        r.set_detail(DETAIL)
        r.clear_detail()
        self.assertIsNone(r.waterfall)
        self.assertIsNone(r.span_table)
        self.assertEqual(r.detail_layout.count(), 0)

    def test_header_shows_operation_and_cost(self):
        r = self._row()
        self.assertEqual(r.operation_label.text(), cost.op_label("resume_generate"))
        self.assertIn("0.07", r.cost_label.text())


class TestCostPageDrilldown(unittest.TestCase):
    def _page(self):
        mw = _FakeMW()
        p1 = mock.patch.object(cost, "LLMWorker", _SyncWorker)
        p2 = mock.patch.object(cost.api, "get_cost_summary", return_value=dict(SUMMARY))
        p3 = mock.patch.object(cost.api, "list_cost_traces",
                               return_value={"items": [dict(t) for t in TRACES], "total": 2})
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        page = cost.CostPage(mw)
        self.addCleanup(page.deleteLater)
        return page

    def test_ctor_does_not_fetch(self):
        with mock.patch.object(cost, "LLMWorker", _SyncWorker), \
             mock.patch.object(cost.api, "get_cost_summary") as summary:
            cost.CostPage(_FakeMW())
        summary.assert_not_called()

    def test_show_event_loads_and_renders_rows(self):
        page = self._page()
        page.showEvent(QShowEvent())
        self.assertEqual(len(page._rows), 2)
        self.assertEqual(page.trace_host.count(), 2)
        self.assertTrue(page.trace_empty.isHidden())

    def test_empty_list_shows_empty_state(self):
        page = self._page()
        page._render_traces([])
        self.assertEqual(page.trace_host.count(), 0)
        self.assertFalse(page.trace_empty.isHidden())
        self.assertFalse(page._rows)

    def test_clicking_a_row_fetches_detail_lazily(self):
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        with mock.patch.object(cost.api, "get_trace_detail",
                               return_value=DETAIL) as detail:
            page._on_trace_toggle("t1")
        detail.assert_called_once_with("t1")
        row = page._rows["t1"]
        self.assertTrue(row.is_open)
        self.assertIsNotNone(row.waterfall)
        self.assertEqual(row.waterfall.spans(), SPANS)

    def test_clicking_again_collapses_without_refetch(self):
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        with mock.patch.object(cost.api, "get_trace_detail", return_value=DETAIL) as detail:
            page._on_trace_toggle("t1")
            page._on_trace_toggle("t1")
        detail.assert_called_once()                 # 收起不该再打接口
        row = page._rows["t1"]
        self.assertFalse(row.is_open)
        self.assertIsNone(row.waterfall)

    def test_accordion_keeps_one_open(self):
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        with mock.patch.object(cost.api, "get_trace_detail", return_value=DETAIL):
            page._on_trace_toggle("t1")
            page._on_trace_toggle("t2")
        self.assertFalse(page._rows["t1"].is_open)
        self.assertIsNone(page._rows["t1"].waterfall)
        self.assertTrue(page._rows["t2"].is_open)
        self.assertEqual(page._expanded_id, "t2")

    def test_unknown_trace_id_is_ignored(self):
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        page._on_trace_toggle("nope")               # 不该抛
        self.assertIsNone(page._expanded_id)

    def test_detail_failure_shows_reason(self):
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        with mock.patch.object(cost.api, "get_trace_detail",
                               side_effect=ApiError("trace 不存在")):
            page._on_trace_toggle("t1")
        row = page._rows["t1"]
        self.assertTrue(row.is_open)
        self.assertIsNone(row.waterfall)
        self.assertEqual(row.detail_layout.count(), 1)

    def test_stale_callback_after_refresh_does_not_crash(self):
        # 刷新会销毁旧行；晚到的回调按 id 查不到就该安静返回
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        page._render_traces([])
        page._fill_trace_detail("t1", DETAIL)       # 不该抛
        page._fail_trace_detail("t1", "x")

    def test_rendered_rows_are_trace_rows(self):
        page = self._page()
        page._render_traces([dict(t) for t in TRACES])
        widgets = [page.trace_host.itemAt(i).widget()
                   for i in range(page.trace_host.count())]
        self.assertTrue(all(isinstance(w, cost.TraceRow) for w in widgets))


if __name__ == "__main__":
    unittest.main(verbosity=2)
