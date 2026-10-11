#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复用闭环：生成/优化**每次必查库** + 成果**自动入库并进复用池**。

承接用户的三个诉求：
  ① 每次生成/优化都调用数据库 → 由 `_load_reference` 的**召回轨迹**给出凭据
  ② 结果自动入库            → 落 `local_store` + 立刻进本地向量索引
  ③ 不断复用、越用越好        → 历史成果作为独立来源（generated/optimized）参与后续召回

分七层：

1. **来源分流**：简历实体里 `source=generated/optimized` 的记录必须进「成果池」，
   其余进「材料池」；质量不过关的成果**降级回材料池**（不进召回，但不丢数据）。
2. **白名单检索**：`source` 支持列表，`["corpus", *WORK_SOURCES]` 天然排除材料池。
3. **回声防护**：成果池里与输入几乎同分（>= 0.95）的块必须被丢弃 —— 否则
   「同一份底稿的历史版本」会被当参考，生成结果和上一版没差别。
4. **双池召回**：`_load_reference` 两路各带配额，返回 trace（corpus_hits / work_hits）。
5. **自动入池**：`save_generated` / `save_optimized` 落库后立刻 `upsert_local`。
6. **重建不重复也不丢**：同一条 id 不许在快照（resume）与成果池（generated）各出一份。
7. **界面可见**：`components.retrieval_summary` 能把轨迹说成人话。

⚠️ 反向对照：`tests/_mutate_retrieval_closure_check.py` 逐条砍掉上述机制并验证本文件变红。

运行：python tests/test_ai_local_retrieval_closure.py
"""

import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop import local_store                      # noqa: E402
from offerflow_desktop.local_ai import index, pull, retrieve   # noqa: E402
from offerflow_desktop.services import resume_generation as RG  # noqa: E402
from offerflow_desktop.services import resume_optimizer as RO   # noqa: E402
from offerflow_desktop.ui.components import retrieval_summary   # noqa: E402

_QUERY = "AI 应用工程师 RAG 向量检索 Python"
_POS = {"position": "AI 应用工程师", "summary": "RAG 与文档智能",
        "skills": ["Python", "RAG"]}

# 假快照：绕开联网拉取，只喂测试语料（结构必须与 pull.py 一致）
_SNAP = {
    "corpus": {"count": 3, "items": [
        {"id": "c1", "item_type": "resume",
         "raw_text": "【求职意向】AI 应用工程师。熟悉 RAG 检索增强、向量数据库、"
                     "Python 后端开发，主导过文档智能问答系统落地，服务数千内部用户。"},
        {"id": "c2", "item_type": "resume",
         "raw_text": "【求职意向】Python 后端开发工程师。精通 FastAPI、PostgreSQL、"
                     "Docker，负责过高并发订单系统，QPS 提升 3 倍。"},
        {"id": "c3", "item_type": "jd",
         "raw_text": "岗位：AI 应用工程师，要求熟悉 RAG、LangChain、向量检索。"},
    ]},
    "resumes": {"count": 0, "items": []},
    "jd": {"count": 0, "items": []},
}

_WORK_TEXT = ("【求职意向】AI 应用开发工程师（RAG / Agent 方向）。\n"
              "【项目经历】文档智能问答系统：基于 RAG 检索增强，用 bge 向量模型做召回，"
              "服务 3000+ 内部用户，平均响应 1.2s。")


def _fake_gen_result(text=_WORK_TEXT, quality=None):
    return {
        "resume_text": text,
        "structured": {"position": "AI 应用工程师"},
        "quality": quality if quality is not None else {"total_score": 88, "passed": True},
        "retrieval": {"query": _QUERY, "corpus_hits": 2, "work_hits": 0},
    }


class _ClosureCase(unittest.TestCase):
    """隔离 `local_store` 与**本地向量索引**（真实 index.db 带着用户自己的语料，不能碰）。"""

    ACCOUNT = "closure-test@local"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="closure_test_")

        self._prev_account = local_store.get_account()
        local_store.set_account(self.ACCOUNT)
        local_store.clear(self.ACCOUNT)

        self._prev_index_db = index.INDEX_DB
        self._prev_store = index._store
        index.INDEX_DB = os.path.join(self._tmp, "index.db")
        index._store = None

        self._prev_load = pull.load_cache
        pull.load_cache = lambda name: _SNAP.get(name)

        # 记忆/图谱各自另有库路径，这里一律关掉（本文件只关心向量索引）
        self._prev_mem, self._prev_graph = retrieve._memory_block, retrieve._graph_block
        retrieve._memory_block = lambda q: {"text": "", "items": [], "reason": "off"}
        retrieve._graph_block = lambda h: {"text": "", "lines": 0, "reason": "off"}

        def _teardown():
            index.INDEX_DB = self._prev_index_db
            index._store = self._prev_store
            pull.load_cache = self._prev_load
            retrieve._memory_block = self._prev_mem
            retrieve._graph_block = self._prev_graph
            local_store.clear(self.ACCOUNT)
            local_store.set_account(self._prev_account)
            shutil.rmtree(self._tmp, ignore_errors=True)

        self.addCleanup(_teardown)
        index.rebuild(do_pull=False)

    # ---- 便捷 ----
    def _by_source(self):
        return index._get_store().stats()["by_source"]

    def _source_of_item(self, item_id):
        """该记录在索引里的 source（块主键形如 `{source}:{id}`）。"""
        for row in index._get_store().all_rows():
            if row["id"].split(":")[-1] == item_id:
                return row["source"]
        return None

    def _works_hits(self, query=_QUERY, top_k=1):
        return retrieve.retrieve_context(query, item_type="resume", top_k=top_k,
                                         sources=list(index.WORK_SOURCES))


# ============ 1. 来源分流 + 质量闸门 ============

class TestWorkSourceRouting(unittest.TestCase):
    def test_generated_and_optimized_go_to_work_pool(self):
        for src in ("generated", "optimized"):
            self.assertEqual(
                index.local_source_of("resumes", {"source": src,
                                                  "parsed_json": {"quality": {"passed": True}}},
                                      "resume"),
                src, "%s 应进成果池" % src)

    def test_imported_stays_in_material_pool(self):
        for src in ("imported", "", "张三.docx", None):
            self.assertEqual(
                index.local_source_of("resumes", {"source": src}, "resume"),
                "resume", "非成果来源必须留在材料池")

    def test_low_quality_work_is_demoted(self):
        """质量不过关的成果 → 降级回材料池（不参与召回，但记录仍在本地主存）。"""
        item = {"source": "generated",
                "parsed_json": {"quality": {"total_score": 40, "passed": False}}}
        self.assertEqual(index.local_source_of("resumes", item, "resume"), "resume")

    def test_missing_quality_still_enters_pool(self):
        """没有质量报告的老成果要放行 —— 闸门只挡「明确不合格」的。"""
        self.assertEqual(
            index.local_source_of("resumes", {"source": "generated"}, "resume"),
            "generated")

    def test_non_resume_entity_never_routed(self):
        self.assertEqual(index.local_source_of("jds", {"source": "generated"}, "jd"), "jd")


# ============ 2. 白名单检索 ============

class TestSourceWhitelist(_ClosureCase):
    def test_whitelist_list_excludes_material_pool(self):
        """材料池不在白名单里 → 结果里一条 resume 都不许出现。"""
        local_store.save("resumes", "create", "mat-1",
                         {"id": "mat-1", "source": "imported",
                          "raw_text": "我对 RAG 向量检索很有经验，做过文档智能问答。"})
        index.rebuild(do_pull=False)
        res = index.search(_QUERY, top_k=10, item_type="resume",
                           source=["corpus", *index.WORK_SOURCES])
        # 先断言「真命中了」再断言「没混进材料池」—— 只断言后者的话，
        # 一个「白名单处理不当直接抛异常、返回空」的实现也是绿的（空靶）。
        self.assertTrue(res["results"], "白名单检索应当至少命中一条语料")
        sources = {r["source"] for r in res["results"]}
        self.assertNotIn("resume", sources)

    def test_single_source_still_works(self):
        res = index.search(_QUERY, top_k=5, item_type="resume", source="corpus")
        self.assertTrue(res["ok"])
        self.assertTrue(all(r["source"] == "corpus" for r in res["results"]))


# ============ 3. 回声防护 ============

class TestEchoGuard(_ClosureCase):
    def test_identical_content_is_dropped_as_echo(self):
        RG.save_generated(_fake_gen_result())
        # 拿「几乎一模一样」的文本去查 → 成果块必须被丢弃
        res = retrieve.retrieve_context(_WORK_TEXT, item_type="resume", top_k=3,
                                        sources=list(index.WORK_SOURCES))
        self.assertEqual(res["hits"], 0, "与自己几乎相同的成果块必须被当回声丢弃")
        self.assertGreaterEqual(res["echo_dropped"], 1)

    def test_echo_ceiling_constant_is_high(self):
        """上限必须足够高（不然正常参考会被误杀），又不能到 1.0（不然形同虚设）。"""
        self.assertGreaterEqual(retrieve.ECHO_SCORE_CEILING, 0.9)
        self.assertLess(retrieve.ECHO_SCORE_CEILING, 1.0)


# ============ 4. 双池召回 + 召回轨迹 ============

class TestLoadReferenceTwoPools(_ClosureCase):
    def test_generation_reference_has_trace(self):
        ref = RG._load_reference(_POS)
        self.assertIsInstance(ref, dict)
        self.assertIn("text", ref)
        trace = ref["trace"]
        self.assertEqual(trace["corpus_hits"], 2, "corpus 路应命中 2 条")

    def test_optimization_reference_has_trace(self):
        jd = RO.build_requirement_from_jd(
            {"position": "AI 应用工程师", "must_have": ["RAG", "Python"],
             "tech_stack": ["Python"]})
        ref = RO._load_reference(jd)
        self.assertIsInstance(ref, dict)
        self.assertGreaterEqual(ref["trace"]["corpus_hits"], 1)

    def test_works_pool_joins_recall_after_save(self):
        """落一份成果后，下一次召回的 trace 里必须出现 work_hits > 0。"""
        before = RG._load_reference(_POS)["trace"]
        self.assertEqual(before["work_hits"], 0, "池空时不该有成果命中")

        RG.save_generated(_fake_gen_result())

        after = RG._load_reference(_POS)["trace"]
        self.assertEqual(after["work_hits"], 1, "落库后成果必须进召回池")

    def test_work_block_is_labeled_in_prompt_text(self):
        RG.save_generated(_fake_gen_result())
        text = RG._load_reference(_POS)["text"]
        self.assertIn("你自己过去生成", text,
                      "成果块必须标注来历，否则模型会把它当第三方资料照抄")


# ============ 5. 自动入池 ============

class TestAutoIndexOnSave(_ClosureCase):
    def test_save_generated_indexes_immediately(self):
        out = RG.save_generated(_fake_gen_result())
        self.assertTrue(out.get("id"))
        self.assertGreaterEqual(self._by_source().get("generated", 0), 1,
                                "生成结果必须立刻进复用池")
        self.assertEqual(self._source_of_item(out["id"]), "generated")

    def test_save_optimized_indexes_immediately(self):
        resume = {"id": "r-1", "raw_text": "张三 某大学 计算机 负责订单系统",
                  "version": 1}
        result = {
            "optimized_text": "【求职意向】AI 应用工程师。精通 RAG 检索增强与向量检索，"
                              "负责文档智能问答系统，服务 3000 用户。",
            "preservation": {"passed": True},
            "ats_after": {"score": 82},
            "retrieval": {"query": _QUERY, "corpus_hits": 1},
        }
        saved = RO.save_optimized(resume, result, target_jd={"position": "AI 应用工程师"})
        self.assertIsNotNone(saved)
        self.assertGreaterEqual(self._by_source().get("optimized", 0), 1,
                                "优化结果必须立刻进复用池")
        self.assertEqual(self._source_of_item(saved["id"]), "optimized")

    def test_low_quality_work_not_in_pool(self):
        out = RG.save_generated(
            _fake_gen_result(quality={"total_score": 40, "passed": False}))
        self.assertEqual(self._source_of_item(out["id"]), "resume",
                         "低质成果不许进复用池")
        self.assertEqual(self._by_source().get("generated", 0), 0)

    def test_not_logged_in_does_not_crash(self):
        local_store.set_account(None)
        out = RG.save_generated(_fake_gen_result())
        self.assertNotIn("traceback", str(out).lower())   # 不抛即可
        self.assertIsNone(out.get("id") or None)


# ============ 6. 重建：既不重复也不丢 ============

class TestRebuildMergesWorks(_ClosureCase):
    def test_rebuild_keeps_works_once(self):
        out = RG.save_generated(_fake_gen_result())
        rep = index.rebuild(do_pull=False)
        self.assertEqual(rep.get("work_items"), 1, "重建应识别出 1 条成果")

        # 同一 id 只能有一份（不许 resume / generated 各来一份）
        rows = [r for r in index._get_store().all_rows()
                if r["id"].split(":")[-1] == out["id"]]
        self.assertEqual(len(rows), 1, "同一条成果在索引里必须只出现一次")
        self.assertEqual(rows[0]["source"], "generated")

    def test_rebuild_then_works_still_recallable(self):
        RG.save_generated(_fake_gen_result())
        index.rebuild(do_pull=False)
        self.assertEqual(self._works_hits()["hits"], 1)

    def test_rebuild_does_not_let_snapshot_reclaim_work(self):
        """成果已上行、又随快照拉回来时，不许被快照那份（source 恒 resume）顶掉。

        `_SNAP["resumes"]` 默认是空的，若只用它，`skip_ids` 传不传都没差别 ——
        这个用例必须**亲手**把「同 id 的快照条目」喂进去，否则 `skip_ids` 失效
        （反向对照 M13）也照样是绿的。
        """
        out = RG.save_generated(_fake_gen_result())
        wid = out["id"]
        pull.load_cache = lambda name: {
            "corpus": _SNAP["corpus"], "jd": _SNAP["jd"],
            # 服务端快照里这条的 source 恒为 resume（它不认识本地的成果池）
            "resumes": {"count": 1, "items": [
                {"id": wid, "item_type": "resume", "raw_text": _WORK_TEXT}]},
        }.get(name)

        rep = index.rebuild(do_pull=False)
        self.assertEqual(rep.get("work_items"), 1, "重建应识别出 1 条成果")

        rows = [r for r in index._get_store().all_rows()
                if r["id"].split(":")[-1] == wid]
        self.assertEqual(len(rows), 1, "快照里的同 id 不许让成果被索引两遍")
        self.assertEqual(rows[0]["source"], "generated",
                         "成果不能被快照以 resume 覆盖回被排除的材料池")


# ============ 7. 界面文案 ============

class TestRetrievalSummary(unittest.TestCase):
    def test_reports_both_pools(self):
        s = retrieval_summary({"corpus_hits": 2, "work_hits": 1})
        self.assertIn("共享范文 2 条", s)
        self.assertIn("你的历史成果 1 条", s)

    def test_reports_empty_hit_honestly(self):
        s = retrieval_summary({"corpus_hits": 0, "work_hits": 0})
        self.assertIn("没有召回到参考语料", s)

    def test_reports_error(self):
        s = retrieval_summary({"corpus_hits": 0, "work_hits": 0, "error": "索引未构建"})
        self.assertIn("索引未构建", s)

    def test_reports_echo(self):
        s = retrieval_summary({"corpus_hits": 1, "work_hits": 0, "echo_dropped": 2})
        self.assertIn("回声", s)

    def test_none_is_empty(self):
        self.assertEqual(retrieval_summary(None), "")
        self.assertEqual(retrieval_summary({}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
