#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简历生成历史 / 版本链（对齐网页版 ResumeGenerate 的「生成历史」卡片）。

对标的网页版行为
----------------
- 历史来源：`api.resume.list()` 里 `parsed_json.kind === "generated"` 且带
  `parsed_json.generation` 的记录，按版本倒序。
- 点一条 = 预览那条；再点 = 收起。
- 每条可删（删除前确认）；删掉正在看的那条要顺手把预览收掉。
- 当前预览那份文档若有多版，显示版本链（历史版本不可修改）。

反向对照（把缺陷放回去，判据必须变红）：
  · 过滤条件只看「有没有 parsed_json」⇒ 优化记录会混进「生成历史」；
  · 版本倒序若漏了 ⇒ 最新那份跑到列表末尾；
  · 删除不清理当前预览 ⇒ 界面上留着一份已经不存在的记录。

⚠️ 本文件不构造 MainWindow、不发任何真实请求：所有网络走桩。

运行：python tests/test_resume_history.py
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtGui import QShowEvent                                # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton      # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import database                               # noqa: E402
from offerflow_desktop.ui.pages import resume_generate as rgen       # noqa: E402


def _item(rid, version, name="张三", position="后端实习", kind="generated"):
    """一条简历记录。`kind` 换成 optimized/uploaded 就应被历史列表挡掉。"""
    generation = {
        "id": rid, "version": version, "document_id": "doc-1",
        "name": name, "position": position, "contact": {},
        "summary": "计算机科学与技术专业。",
        "education": [{"school": "北邮", "major": "计算机", "degree": "本科",
                       "start": "2023-09", "end": "2027-06"}],
        "experiences": [{"type": "project", "name": "OfferFlow", "role": "后端",
                         "start": "2024-01", "end": "2024-06",
                         "bullets": ["负责接口开发与联调，补齐异常处理"]}],
        "skills": ["Python"], "certifications": [],
        "resume_text": "张三 后端实习",
        "ats": {"score": 88}, "quality": {"total_score": 90, "passed": True},
        "gap": {}, "fidelity": {}, "sufficiency": {}, "tips": [],
    }
    return {
        "id": rid, "version": version, "raw_text": "张三 后端实习",
        "source_file": "AI 生成", "created_at": "2026-10-08T10:00:00",
        "parsed_json": {"kind": kind, "generation": generation,
                        "structured": {k: generation[k] for k in (
                            "name", "position", "contact", "summary", "education",
                            "experiences", "skills", "certifications")}},
    }


class TestHistoryHelpers(unittest.TestCase):

    def test_filter_keeps_only_generated(self):
        items = [_item("a", 1), _item("b", 2, kind="optimized"),
                 _item("c", 3, kind="uploaded"),
                 {"id": "d", "version": 4, "parsed_json": None},
                 {"id": "e", "version": 5, "parsed_json": {"kind": "generated"}}]
        got = [x["id"] for x in rgen._generated_history(items)]
        self.assertEqual(got, ["a"],
                         "只有 kind=generated 且带 generation 的记录才算生成历史")

    def test_sorted_by_version_desc(self):
        items = [_item("a", 1), _item("c", 3), _item("b", 2)]
        got = [x["id"] for x in rgen._generated_history(items)]
        self.assertEqual(got, ["c", "b", "a"], "最新版本要排在前面")

    def test_tolerates_junk(self):
        self.assertEqual(rgen._generated_history(None), [])
        self.assertEqual(rgen._generated_history([None, 1, "x"]), [])

    def test_label_prefers_position(self):
        self.assertEqual(rgen._history_label(_item("a", 2)), "后端实习 · v2")
        no_pos = _item("a", 2, position="")
        self.assertEqual(rgen._history_label(no_pos), "张三 · v2",
                         "没岗位就用姓名兜底")

    def test_result_from_history_fills_structured_and_scores(self):
        r = rgen._result_from_history(_item("a", 2))
        self.assertEqual(r["id"], "a")
        self.assertEqual(r["version"], 2)
        self.assertEqual(r["ats"]["score"], 88, "评分要能带到预览里")
        self.assertEqual(r["review"]["total_score"], 90)
        self.assertEqual(r["structured"]["name"], "张三")

    def test_result_from_history_survives_missing_structured(self):
        """旧记录可能没有 structured —— 要用 generation 的扁平字段补出来。"""
        it = _item("a", 2)
        it["parsed_json"].pop("structured")
        r = rgen._result_from_history(it)
        self.assertEqual(r["structured"]["name"], "张三")
        self.assertIn("Python", r["structured"]["skills"])


class _IsolatedDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offflow_hist_")
        self._saved = (database.DB_PATH, database._conn,
                       database._conn_path, database._active_user)
        database._conn = None
        database._conn_path = None
        database._active_user = None
        database.DB_PATH = os.path.join(self.tmp, "isolated.db")
        database.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        (database.DB_PATH, database._conn,
         database._conn_path, database._active_user) = self._saved

    def _page(self, items=None):
        mw = types.SimpleNamespace(refresh_status=lambda: None,
                                   set_status=lambda *a, **k: None)
        page = rgen.ResumeGeneratePage(mw)
        self.addCleanup(page.setParent, None)
        # 离线桩：任何联网都瞬间返回，避免单测去连真实服务端
        rgen.db.list_resumes = lambda: []
        rgen.db.list_resume_versions = lambda _rid: []
        rgen.db.delete_resume = lambda _rid: None
        if items is not None:
            page._history = rgen._generated_history(items)
            page._render_history()
        return page

    def _pump(self):
        from offerflow_desktop.llm import _ALIVE
        for _ in range(200):
            _APP.processEvents()
            if not _ALIVE:
                break

    def _chips(self, page):
        """控件树里的历史条目按钮（chip 类，不含删除 ×）。"""
        return [b for b in page.history_host.findChildren(QPushButton)
                if b.property("class") == "chip" and b.text() != "×"]


class TestHistoryUI(_IsolatedDb):

    def test_ctor_does_not_load_history(self):
        """构造期不许发请求（本项目硬规矩；静态审计也管这条）。"""
        page = self._page()
        self.assertIsNone(page._history_worker, "构造期不该已经起了拉取线程")

    def test_empty_history_shows_hint(self):
        page = self._page(items=[])
        texts = [page.history_layout.itemAt(i).widget().text()
                 for i in range(page.history_layout.count())
                 if hasattr(page.history_layout.itemAt(i).widget(), "text")]
        self.assertTrue(any("还没有生成记录" in t for t in texts), texts)

    def test_one_chip_per_record(self):
        page = self._page(items=[_item("a", 1), _item("b", 2)])
        btns = self._chips(page)
        self.assertEqual(len(btns), 2, "两条记录应有两个 chip")

    def test_optimized_record_never_shows_up(self):
        """反向对照：优化记录混进「生成历史」就是数据来源搞错了。"""
        page = self._page()
        page._on_history_loaded([_item("a", 1), _item("b", 2, kind="optimized")])
        self.assertEqual([b.text() for b in self._chips(page)],
                         ["后端实习 · v1"])

    def test_toggle_previews_then_collapses(self):
        page = self._page(items=[_item("a", 1)])
        page._toggle_history_item(page._history[0])
        self.assertEqual((page._current_result or {}).get("id"), "a", "点开应预览")
        self.assertTrue(self._chips(page)[0].isChecked(), "正在看的那条要高亮")
        page._toggle_history_item(page._history[0])
        self.assertIsNone(page._current_result, "再点应收起")
        self.assertFalse(self._chips(page)[0].isChecked())

    def test_version_chain_renders_and_switches(self):
        page = self._page(items=[_item("a", 2)])
        page._versions = [
            {"id": "a", "version": 2, "raw_text": "x", "parsed_json": _item("a", 2)["parsed_json"]},
            {"id": "a1", "version": 1, "raw_text": "y", "parsed_json": _item("a1", 1)["parsed_json"]},
        ]
        page._current_result = rgen._result_from_history(page._history[0])
        page._render_history()
        self.assertIn("当前简历版本", self._history_text(page), "多版时要显示版本链")
        # 点旧版本 → 预览切过去（只读，不改任何数据）
        page._view_version(page._versions[1])
        self.assertEqual(page._current_result["id"], "a1")
        self.assertEqual(page._current_result["version"], 1)

    def test_delete_clears_active_preview(self):
        page = self._page(items=[_item("a", 1), _item("b", 2)])
        page._toggle_history_item(page._history[0])       # 正在看 b
        self.assertEqual(page._current_result["id"], "b")
        page._on_history_deleted(page._history[0])
        self.assertEqual(len(page._history), 1, "删掉的那条要从列表里消失")
        self.assertIsNone(page._current_result, "正在看的那条被删了要收起预览")

    def test_delete_inactive_keeps_preview(self):
        page = self._page(items=[_item("a", 1), _item("b", 2)])
        page._toggle_history_item(page._history[0])       # 正在看 b
        page._on_history_deleted(page._history[1])        # 删掉 a
        self.assertEqual(page._current_result["id"], "b", "没在看的那条被删不动预览")

    def test_show_event_loads_history_once(self):
        page = self._page()
        self.assertIsNone(page._history_worker)
        page.showEvent(QShowEvent())
        self._pump()
        self.assertIsNotNone(page._history_worker, "showEvent 应触发一次拉取")
        first = page._history_worker
        page.showEvent(QShowEvent())
        self._pump()
        self.assertIs(page._history_worker, first, "重复 show 不该重复拉取")

    def test_load_failure_is_shown_not_swallowed(self):
        page = self._page()
        page._on_history_failed("网络不可用")
        self.assertIn("网络不可用", self._history_text(page),
                      "拉取失败要如实显示，不能静默当空列表")

    # ---- 工具 ----
    def _history_text(self, page) -> str:
        """把历史卡片区域里所有可见文字拼起来（用于断言提示/版本链文案）。"""
        parts = []
        for w in page.history_host.findChildren(QLabel):
            if w.text():
                parts.append(w.text())
        return " | ".join(parts)


class TestGenerateWiring(_IsolatedDb):

    def test_on_generated_adds_history_entry(self):
        """生成完立刻能在历史里看到（不等重进页面）。"""
        page = self._page()
        page._last_req = {"name": "张三"}
        page._on_generated({"id": "new-1", "version": 1,
                            "structured": {"name": "张三", "position": "后端实习",
                                           "education": [], "experiences": [],
                                           "skills": [], "certifications": [],
                                           "contact": {}, "summary": ""},
                            "resume_text": "x", "ats": {}, "gap": {}, "fidelity": {},
                            "review": {}, "sufficiency": {}, "tips": []})
        self._pump()
        self.assertEqual(len(page._history), 1)
        self.assertEqual(page._history[0]["id"], "new-1")
        self.assertIn("v1", rgen._history_label(page._history[0]))

    def test_api_generate_result_carries_version(self):
        """接口层前提：不带 version 就没法显示 vN / 做版本切换。"""
        import inspect
        src = inspect.getsource(rgen.db.generate_resume)
        self.assertIn('"version"', src, "generate_resume 应把 version 透出来")
        self.assertIn('"document_id"', src, "generate_resume 应把 document_id 透出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
