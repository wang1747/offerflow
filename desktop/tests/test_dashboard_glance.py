#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首页「最近投递」与「匹配速览」的判据（G10）。

判据分三层：
  1. **条数截断**：最近投递最多 6 条、匹配速览最多 4 条（对齐网页版
     `apps.slice(0, 6)` / `rankings.slice(0, 4)`）—— 不截断会把首页撑到很长；
  2. **分数着色**：`score >= 80` / `>= 60` / 其余 三档分别落到
     score-good / score-mid / score-bad（语义 class 由 QSS 供色，深浅主题各自校准）；
  3. **空态与时机**：没有匹配记录时整卡不占位；两块速览都在 `showEvent` 里加载，
     **构造期不发请求**（构造期起线程 → 页面销毁时回调摸到已删除的 C++ 对象，
     进程无 traceback 崩退，已实测）。

反向对照见 `_mutate_p1_check.py`。

运行：python tests/test_dashboard_glance.py [TestCase.test_method]
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
from PySide6.QtWidgets import (                                      # noqa: E402
    QApplication, QLabel, QPushButton, QWidget,
)

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import llm as llm_mod                         # noqa: E402
from offerflow_desktop.config import STATUS_LABELS                   # noqa: E402
from offerflow_desktop.ui.pages import dashboard                     # noqa: E402

STATS = {"resume_count": 2, "jd_count": 3, "total": 5, "article_count": 1,
         "optimized_count": 1,
         "conversion_rate": {"applied_to_interview": 20, "interview_to_offer": 50}}

APPS = [
    {"id": "a%d" % i, "company": "公司%d" % i, "position": "岗位%d" % i,
     "status": "applied", "updated_at": "2026-10-0%dT10:00:00" % (i % 9 + 1)}
    for i in range(1, 9)          # 8 条：用来验证「只显示 6 条」
]
APPS[0]["status"] = "first_interview"

RANKINGS = [
    {"company": "字节跳动", "position": "RAG 算法", "score": 85,
     "suggestion": "重点补 RAG 检索链路"},
    {"company": "合合信息", "position": "文档智能", "score": 62,
     "suggestion": "补 OCR 与版面分析经验"},
    {"company": "某创业公司", "position": "NLP", "score": 40,
     "suggestion": "差距较大，建议先补基础"},
    {"company": "美团", "position": "搜广推", "score": 71,
     "suggestion": "补推荐系统项目"},
    {"company": "第五家", "position": "不该显示", "score": 90,
     "suggestion": "超出 4 条以外"},
]


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
    def __init__(self):
        super().__init__()
        self.navigated = []

    def navigate(self, *args, **kwargs):
        self.navigated.append(args[0] if args else None)

    def refresh_status(self):
        pass

    def open_agent(self, *args, **kwargs):
        pass


def _row_labels(layout):
    """布局里每一行控件的文字集合（用于断言"渲染了什么"）。"""
    out = []
    for i in range(layout.count()):
        w = layout.itemAt(i).widget()
        if w is None:
            continue
        out.append([lbl.text() for lbl in w.findChildren(QLabel)])
    return out


def _badge_classes(layout):
    """匹配速览里各行的评分徽章 class（score-good / -mid / -bad）。"""
    out = []
    for i in range(layout.count()):
        w = layout.itemAt(i).widget()
        if w is None:
            continue
        for lbl in w.findChildren(QLabel):
            cls = str(lbl.property("class") or "")
            if cls.startswith("score-"):
                out.append(cls)
    return out


class _Base(unittest.TestCase):
    def _build_page(self, apps=None, rankings=None):
        mw = _FakeMW()
        patches = [
            mock.patch.object(dashboard.db, "get_statistics", return_value=dict(STATS)),
            mock.patch.object(dashboard.db, "list_applications",
                              return_value=[dict(a) for a in (apps if apps is not None else APPS)]),
            mock.patch.object(dashboard.db, "list_matches",
                              return_value=[dict(r) for r in (rankings if rankings is not None else RANKINGS)]),
            # showEvent 里的提醒走 LLMWorker，换成同步假实现（否则真打接口 + 起线程）
            mock.patch.object(llm_mod, "LLMWorker", _SyncWorker),
            mock.patch.object(dashboard.db, "list_reminders",
                              return_value={"overdue": [], "upcoming": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        page = dashboard.DashboardPage(mw)
        self.addCleanup(page.deleteLater)
        return page, mw


class TestRecentApplications(_Base):
    def test_ctor_shows_empty_state_without_fetching_matches(self):
        with mock.patch.object(dashboard.db, "get_statistics", return_value=dict(STATS)), \
             mock.patch.object(dashboard.db, "list_applications", return_value=[]), \
             mock.patch.object(dashboard.db, "list_matches") as matches:
            page = dashboard.DashboardPage(_FakeMW())
            self.addCleanup(page.deleteLater)
        matches.assert_not_called()                 # 构造期不拉匹配
        self.assertEqual(page.recent_host.count(), 1)   # 只有空态提示
        self.assertIn("暂无投递记录", page.recent_host.itemAt(0).widget().text())

    def test_show_event_renders_recent(self):
        page, _mw = self._build_page()
        page.showEvent(QShowEvent())
        self.assertEqual(page.recent_host.count(), dashboard.RECENT_LIMIT)

    def test_recent_is_truncated_to_limit(self):
        page, _mw = self._build_page()
        page._render_recent([dict(a) for a in APPS])      # 给 8 条
        self.assertEqual(page.recent_host.count(), dashboard.RECENT_LIMIT)
        texts = " ".join(" ".join(cells) for cells in _row_labels(page.recent_host))
        self.assertIn("岗位1", texts)
        self.assertNotIn("岗位7", texts)                   # 第 7 条起不显示

    def test_rows_show_company_position_and_status_chip(self):
        page, _mw = self._build_page()
        page._render_recent([dict(APPS[0])])
        cells = _row_labels(page.recent_host)[0]
        self.assertTrue(any("公司1" in c and "岗位1" in c for c in cells))
        self.assertIn(STATUS_LABELS["first_interview"], cells)
        self.assertTrue(any("2026-10-0" in c for c in cells))

    def test_missing_company_gets_placeholder(self):
        page, _mw = self._build_page()
        page._render_recent([{"id": "x", "company": "", "position": "",
                              "status": "applied", "updated_at": ""}])
        cells = _row_labels(page.recent_host)[0]
        self.assertTrue(any("未填公司" in c for c in cells))

    def test_empty_list_returns_to_empty_state(self):
        page, _mw = self._build_page()
        page._render_recent([dict(a) for a in APPS])
        page._render_recent([])
        self.assertEqual(page.recent_host.count(), 1)
        self.assertIn("暂无投递记录", page.recent_host.itemAt(0).widget().text())

    def test_view_all_button_navigates_to_tracker(self):
        page, mw = self._build_page()
        buttons = [b for b in page.findChildren(QPushButton) if b.text() == "查看全部"]
        self.assertTrue(buttons, "「最近投递」卡片上应当有「查看全部」")
        buttons[0].click()
        self.assertIn("投递追踪", mw.navigated)


class TestMatchGlance(_Base):
    def test_card_hidden_when_no_rankings(self):
        page, _mw = self._build_page(rankings=[])
        page.showEvent(QShowEvent())
        self.assertTrue(page.match_card.isHidden())
        self.assertEqual(page.match_host.count(), 0)

    def test_card_visible_and_truncated_to_limit(self):
        page, _mw = self._build_page()
        page.showEvent(QShowEvent())
        self.assertFalse(page.match_card.isHidden())
        self.assertEqual(page.match_host.count(), dashboard.MATCH_LIMIT)
        texts = " ".join(" ".join(c) for c in _row_labels(page.match_host))
        self.assertIn("字节跳动", texts)
        self.assertNotIn("不该显示", texts)            # 第 5 条起不显示

    def test_score_bands_map_to_semantic_classes(self):
        page, _mw = self._build_page()
        page._render_match([dict(r) for r in RANKINGS])
        self.assertEqual(_badge_classes(page.match_host),
                         ["score-good", "score-mid", "score-bad", "score-mid"])

    def test_score_value_is_rendered(self):
        page, _mw = self._build_page()
        page._render_match([dict(RANKINGS[0])])
        cells = _row_labels(page.match_host)[0]
        self.assertIn("85", cells)

    def test_missing_company_position_get_placeholders(self):
        page, _mw = self._build_page()
        page._render_match([{"company": "", "position": "", "score": 0,
                             "suggestion": ""}])
        cells = _row_labels(page.match_host)[0]
        self.assertTrue(any("未知公司" in c and "未知职位" in c for c in cells))

    def test_becoming_empty_hides_card_again(self):
        page, _mw = self._build_page()
        page._render_match([dict(r) for r in RANKINGS])
        self.assertFalse(page.match_card.isHidden())
        page._render_match([])
        self.assertTrue(page.match_card.isHidden())

    def test_view_all_button_navigates_to_job_match(self):
        page, mw = self._build_page()
        page._render_match([dict(RANKINGS[0])])
        buttons = [b for b in page.findChildren(QPushButton) if b.text() == "查看全部"]
        self.assertTrue(buttons, "「匹配速览」卡片上应当有「查看全部」")
        buttons[-1].click()
        self.assertIn("岗位分析", mw.navigated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
