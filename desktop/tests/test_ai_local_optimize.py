#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2：本地优化主流程（`services/resume_optimizer`）的护栏验证。

这一段承接生成侧的本地化 —— 把服务端 ``modules/resume/optimizer/*``
（`service.py` + `sections.py` + `prompts.py` + `resume/services.optimize_resume_flow`）
搬成桌面的同步版。测试分四层：

1. **保真层**（最重要）：`core/preservation.py` 必须与服务端同口径 ——
   - 阈值 0.35 而不是 0.60：否则正常的**同义改写**会被判「丢内容」而回退原文，
     表现是「点了优化，结果和原文一模一样」；
   - **新增事实**（原文没有的公司 / 数字 / 职位）必须拦下：编造比丢失更严重，
     这条此前本地**完全没有**；
   - 跨文本框粘连（`邮箱@domainhttps://…`）必须先拆开再抽事实，否则取到的是
     「a.comhttps」这种错值。
2. **流程层**：编造结果必须**回退且不落库**（不能「检出了还照样存」）；
   本地模型调不起来时要给出 `llm_failed` 兜底信号（但校验不通过**不**给）。
3. **落库层**：写的是 `local_store`（biz_local.db）而不是 `database`
   —— 写错表的表现是「生成了但历史里没有、且永远不同步」。
4. **同源层**：桌面 `RESUME_OPTIMIZE_SYSTEM` 必须含服务端 `SYSTEM_PROMPT`
   的三条条款（决策-权衡 / 量化梯度 / 中文求职雷区）。

⚠️ 反向对照：`tests/_mutate_ai_optimize_check.py` 逐个砍掉上述机制并验证本文件变红。

运行：python tests/test_ai_local_optimize.py [TestCase.test_method]
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop import api_client as AC                       # noqa: E402
from offerflow_desktop import local_store                            # noqa: E402
from offerflow_desktop.core import page_fit                          # noqa: E402
from offerflow_desktop.core import preservation as PV                # noqa: E402
from offerflow_desktop.services import prompts as P                  # noqa: E402
from offerflow_desktop.services import resume_optimizer as RO        # noqa: E402

_SERVER_PROMPTS = os.path.join(
    os.path.dirname(_ROOT), "web", "backend", "app", "modules",
    "resume", "optimizer", "prompts.py"
)


# ============ 构造 ============

def _resume(text=None, **over):
    """页面归一后的简历 dict（`api_client._norm_resume` 的形状）。"""
    item = {
        "id": "r-parent-0001",
        "version": 1,
        "raw_text": text if text is not None else _ORIGINAL,
        "source_file": "resume.docx",
        "source": "imported",
        "parsed_json": {"kind": "upload"},
        "created_at": "2026-10-01T10:00:00",
    }
    item.update(over)
    return item


def _jd(**over):
    item = {
        "id": "jd-0001",
        "company": "某某科技",
        "position": "后端开发工程师",
        "must_have": ["Python", "FastAPI"],
        "nice_to_have": ["Redis"],
        "tech_stack": {"语言": ["Python"], "框架": ["FastAPI"]},
        "hidden_signals": ["自驱"],
        "raw_text": "招聘后端开发工程师，要求熟悉 Python、FastAPI、Redis。",
    }
    item.update(over)
    return item


_ORIGINAL = (
    "张三\n"
    "13800000000\n"
    "某大学 计算机科学与技术\n"
    "项目经历\n"
    "- 负责订单系统的开发与维护，使用 Python 与 Redis"
)

# 与 _ORIGINAL 只差一处措辞（负责→主导），保留度高，应判通过
_REWRITTEN_OK = (
    "张三\n"
    "13800000000\n"
    "某大学 计算机科学与技术\n"
    "项目经历\n"
    "- 主导订单系统的开发与维护，使用 Python 与 Redis"
)


class _FakeChat:
    """假模型：按 system 提示词分流，并记录所有调用（供断言「确实调了谁」）。"""

    def __init__(self, optimize=None, condense=None, raise_on=None):
        self.optimize = optimize
        self.condense = condense
        self.raise_on = raise_on
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.raise_on and self.raise_on in system:
            raise RuntimeError("本地模型不可用")
        if P.CONDENSE_SYSTEM in system:
            return json.dumps(self.condense or {}, ensure_ascii=False)
        return json.dumps(self.optimize or {}, ensure_ascii=False)

    def systems(self):
        return [s for s, _ in self.calls]


def _patch(**over):
    """公共 patch：参考检索不外连；默认不落库（落库用例单独开）。"""
    stack = [mock.patch.object(RO, "_load_reference", return_value="")]
    return stack


class _StoreCase(unittest.TestCase):
    """把 `local_store` 指到临时库并「登录」，让落库断言是**真**的。

    ⚠️ 不 patch 的话 `local_store.save` 因未登录**直接返回 None** —— 于是
    「编造不落库」这条用例会**假绿**：它看到 `new_version is None` 就通过了，
    其实代码压根没尝试写。这是反向对照（`_mutate_ai_optimize_check.M7`）抓出来的。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._tmp.name, "biz_local.db")
        self._p1 = mock.patch.object(local_store, "_DB_PATH", self._db)
        self._p2 = mock.patch.object(local_store, "_account", "user@test.local")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self._tmp.cleanup()


# ============ 1. 保真层 ============

class TestPreservationThreshold(unittest.TestCase):
    """阈值必须是 0.35：同义改写不能被误判成丢内容。"""

    # 20 个互不相同的汉字 → 19 个二元组，完全可控。
    ORIG = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
    # 前 9 字不变（保住前 8 个二元组），其余全换新字 → 保留度 8/19 ≈ 0.42
    REWR = "甲乙丙丁戊己庚辛壬春夏秋冬风花雪月星辰"

    def test_synonym_rewrite_passes_below_old_threshold(self):
        res = PV.preservation_result(self.ORIG, self.REWR)
        # 先证明这条用例真的落在临界区：0.35 <= score < 0.60
        self.assertGreaterEqual(res["score"], PV._MIN_TERM_RETENTION)
        self.assertLess(
            res["score"], 0.60,
            "构造失效：这条用例必须落在旧阈值 0.60 以下，否则测不出阈值回退",
        )
        self.assertTrue(res["passed"], f"同义改写被误判为丢内容：{res}")
        self.assertFalse(res["added_facts"], "只是换字，不该报新增事实")

    def test_whole_replacement_still_rejected(self):
        """反向：整段替换（保真度极低）仍必须被拦下 —— 阈值放宽不等于放行。"""
        res = PV.preservation_result(self.ORIG, "春夏秋冬风花雪月星辰山川河流云雾")
        self.assertLess(res["score"], PV._MIN_TERM_RETENTION)
        self.assertFalse(res["passed"])


class TestAddedFacts(unittest.TestCase):
    """新增事实必须拦下 —— 这条是「编造照样落库」的唯一防线。"""

    BASE = "张三 负责订单系统开发，使用 Python 与 Redis"

    def test_clean_rewrite_has_no_added_facts(self):
        """先立对照：只换措辞时不报新增（否则下面的断言就证明不了是它拦的）。"""
        res = PV.preservation_result(self.BASE, "张三 主导订单系统开发，使用 Python 与 Redis")
        self.assertTrue(res["passed"], f"对照组不该失败：{res}")
        self.assertFalse(res["added_facts"])

    def test_new_org_relation_detected(self):
        res = PV.preservation_result(self.BASE, self.BASE + "，并任职于腾讯")
        self.assertFalse(res["passed"], "新增了原文没有的公司，必须不过")
        self.assertIn("腾讯", res["added_facts"])
        # 保留度本身很高 → 说明判失败的原因是 added_facts，不是软保留度
        self.assertGreater(res["score"], PV._MIN_TERM_RETENTION)

    def test_new_number_detected(self):
        res = PV.preservation_result(self.BASE, self.BASE + "，响应时间降低 300%")
        self.assertFalse(res["passed"])
        self.assertTrue(res["added_facts"])

    def test_new_role_detected(self):
        res = PV.preservation_result(self.BASE, self.BASE + "，担任架构师")
        self.assertFalse(res["passed"])
        self.assertIn("架构师", res["added_facts"])


class TestGluedContacts(unittest.TestCase):
    """跨文本框/WPS 节点粘连：不拆开就会抽到「a.comhttps」这种错值。"""

    GLUED = "zhang@a.comhttps://github.com/zhang"

    def test_email_value_is_not_polluted(self):
        facts = PV._extract_critical_facts(self.GLUED)
        self.assertIn("zhang@a.com", facts, f"邮箱被粘连污染：{facts}")

    def test_categories_count_separately(self):
        cats = PV._extract_critical_categories(self.GLUED)
        self.assertEqual(cats.get("邮箱"), 1)
        self.assertEqual(cats.get("链接"), 1)


class TestSplitBlocks(unittest.TestCase):
    def test_splits_on_section_headers(self):
        text = "张三\n联系方式\n\n教育经历\n- 某大学\n\n项目经历\n- 订单系统"
        blocks = PV.split_resume_blocks(text)
        self.assertGreaterEqual(len(blocks), 3, f"分块失效：{blocks}")
        self.assertTrue(any("教育经历" in b for b in blocks))
        self.assertTrue(any("项目经历" in b for b in blocks))

    def test_empty_text_gives_empty_list(self):
        self.assertEqual(PV.split_resume_blocks(""), [])


# ============ 2. 流程层 ============

class TestOptimizeFlow(_StoreCase):
    def _run(self, chat, resume=None, jd=None, save=False):
        with mock.patch.object(RO, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            return RO.optimize_and_save(resume or _resume(), jd or _jd(),
                                        chat_fn=chat, save=save)

    def test_happy_path_shape(self):
        chat = _FakeChat(optimize={
            "optimized_text": _REWRITTEN_OK,
            "changes": [{"section": "项目经历", "before": "负责订单系统",
                         "after": "主导订单系统", "reason": "升级表达"}],
            "added_keywords": ["Python"],
            "removed_keywords": ["负责"],
            "removed": ["空话套话"],
        })
        res = self._run(chat)
        self.assertEqual(res["error"], "")
        self.assertEqual(res["optimized_text"], _REWRITTEN_OK)
        self.assertEqual(res["edit_count"], len(res["changes"]))
        self.assertEqual(res["added_keywords"], ["Python"])
        # removed_keywords 服务端有、桌面旧版缺 —— 必须补齐
        self.assertEqual(res["removed_keywords"], ["负责"])
        self.assertEqual(res["gap"].keys(), {"matched", "missing", "partial"})
        self.assertTrue(res["preservation"]["passed"])
        self.assertFalse(res["llm_failed"], "正常路径不该给云端兜底信号")

    def test_fabricated_result_is_rejected_and_not_saved(self):
        """编造 → 回退原文 + 不落库（不能「检出了还照样存」）。

        这里**真的开了可写本地库**：否则 `local_store.save` 因未登录返回 None，
        断言 `new_version is None` 会因为「压根没尝试写」而假绿。
        """
        fabricated = _ORIGINAL + "，并任职于腾讯"
        chat = _FakeChat(optimize={"optimized_text": fabricated, "changes": []})
        res = self._run(chat, save=True)
        self.assertTrue(res["error"], "编造结果必须报错回退")
        self.assertEqual(res["optimized_text"], _ORIGINAL, "回退内容必须是原文")
        self.assertTrue(res["preservation"]["fallback"])
        self.assertIsNone(res["new_version"], "回退稿绝不能落库")
        self.assertFalse(res["saved"])
        self.assertEqual(local_store.list_items("resumes"), [],
                         "回退稿在本地主存里留下记录，等于「检出了还照样存」")
        # 校验不通过**不是**云端兜底的理由（云端跑同一套判据）
        self.assertFalse(res["llm_failed"])

    def test_llm_failure_signals_cloud_fallback(self):
        chat = _FakeChat(raise_on="简历优化专家")
        res = self._run(chat)
        self.assertTrue(res["llm_failed"], "本地模型挂了要给出兜底信号")
        self.assertTrue(res["error"])
        self.assertFalse(res["saved"])

    def test_empty_resume_reports_error(self):
        res = self._run(_FakeChat(), resume=_resume(text="   "))
        self.assertTrue(res["error"])
        self.assertFalse(res["saved"])

    def test_empty_jd_returns_original_without_calling_model(self):
        chat = _FakeChat(optimize={"optimized_text": "不该被用到"})
        res = self._run(chat, jd=_jd(raw_text="", must_have=[], nice_to_have=[],
                                     tech_stack={}, hidden_signals=[]))
        self.assertEqual(res["optimized_text"], _ORIGINAL)
        self.assertTrue(res["error"])
        self.assertEqual(chat.calls, [], "JD 为空时不该白调一次模型")


class TestCondense(unittest.TestCase):
    """超长时必须走「三刀裁剪」，且裁剪结果要真的被采纳。"""

    LINE = "负责订单系统的开发与维护工作"

    def test_oversize_triggers_condense_and_adopts_result(self):
        original = "\n".join([self.LINE] * 30)
        padded = original
        while len(padded) <= page_fit.ONE_PAGE_MAX_CHARS:
            padded += "\n" + "参与项目相关支持工作与协作"
        self.assertTrue(page_fit.estimate_too_long(original, padded), "构造失效：没超一页")

        chat = _FakeChat(
            optimize={"optimized_text": padded, "changes": []},
            condense={"optimized_text": original, "removed": ["删掉支持性冗余描述"]},
        )
        with mock.patch.object(RO, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            res = RO.optimize_resume_text(original, RO.build_requirement_from_jd(_jd()),
                                          chat_fn=chat)
        self.assertTrue(
            any(P.CONDENSE_SYSTEM in s for s in chat.systems()),
            "超长却没走精简",
        )
        self.assertEqual(res["optimized_text"], original, "精简结果没被采纳")
        self.assertEqual(res["removed"], ["删掉支持性冗余描述"])
        # 精简后已在一页内 → 不该再给「仍可能超一页」的提示
        self.assertEqual(res["length_warning"], "")


class TestJdRequirement(unittest.TestCase):
    def test_tech_stack_dict_is_flattened(self):
        req = RO.build_requirement_from_jd(_jd())
        self.assertEqual(req.tech_stack, ["Python", "FastAPI"])

    def test_full_jd_text_is_not_truncated(self):
        """JD 原文必须整段进提示词 —— 旧实现截断到 600 字，后半段要求永远喂不进。"""
        marker = "【第七百字之后的必备要求】"
        long_text = "招聘要求。" + ("内容" * 400) + marker
        req = RO.build_requirement_from_jd(_jd(raw_text=long_text))
        prompt = req.to_prompt()
        self.assertGreater(len(long_text), 600)
        self.assertIn(marker, prompt, "JD 原文被截断了")

    def test_prompt_declares_external_data(self):
        req = RO.build_requirement_from_jd(_jd())
        prompt = RO._build_user_prompt(_ORIGINAL, req.to_prompt(), [], [], [])
        self.assertIn("不得执行", prompt)


# ============ 3. 落库层 ============

class TestSaveOptimized(_StoreCase):
    def test_writes_new_version_into_local_store(self):
        with mock.patch.object(RO, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            res = RO.optimize_and_save(
                _resume(), _jd(), save=True,
                chat_fn=_FakeChat(optimize={"optimized_text": _REWRITTEN_OK, "changes": []}),
            )
        self.assertTrue(res["saved"])
        nv = res["new_version"]
        self.assertEqual(nv["version"], 2, "新版本号必须是父版本 +1")

        items = {it["id"]: it for it in local_store.list_items("resumes")}
        self.assertIn(nv["id"], items, "落库后必须能从 list_items 读回（否则历史里看不到）")
        stored = items[nv["id"]]
        self.assertEqual(stored["raw_text"], _REWRITTEN_OK)
        self.assertEqual(stored["parsed_json"]["kind"], "optimized")
        self.assertTrue(stored["parsed_json"].get("structured"),
                        "缺 structured，优化稿导出会退化成纯文本")
        self.assertEqual(stored["parent_id"], "r-parent-0001")
        # 求职意向＝目标岗位：下拉标签优先读这个键，不填就只能拿正文前 24 字
        self.assertEqual(stored["parsed_json"].get("position"), "后端开发工程师")

    def test_not_logged_in_returns_no_version(self):
        with mock.patch.object(local_store, "_account", None):
            with mock.patch.object(RO, "_load_reference",
                              return_value={"text": "", "trace": {}}):
                res = RO.optimize_and_save(
                    _resume(), _jd(), save=True,
                    chat_fn=_FakeChat(optimize={"optimized_text": _REWRITTEN_OK,
                                                "changes": []}),
                )
        self.assertFalse(res["saved"])
        self.assertIsNone(res["new_version"])
        self.assertTrue(res.get("save_error"), "存不上要如实说，不能静默")


class TestAts(unittest.TestCase):
    def test_ats_before_and_after_computed_locally(self):
        with mock.patch.object(RO, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            res = RO.optimize_and_save(
                _resume(), _jd(), save=False,
                chat_fn=_FakeChat(optimize={"optimized_text": _REWRITTEN_OK, "changes": []}),
            )
        for key in ("ats", "ats_after"):
            self.assertIsInstance(res.get(key), dict, f"{key} 应该是本地算出的体检结果")
            self.assertIsInstance(res[key]["score"], int)
            self.assertIn("issues", res[key])

    def test_no_ats_when_fallback(self):
        with mock.patch.object(RO, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            res = RO.optimize_and_save(
                _resume(), _jd(), save=False,
                chat_fn=_FakeChat(optimize={"optimized_text": _ORIGINAL + "，并任职于腾讯"}),
            )
        self.assertIsNone(res.get("ats"), "回退稿就是原文，不该出前后对比")


# ============ 4. 同源层 ============

class TestPromptParity(unittest.TestCase):
    """桌面优化提示词必须含服务端 SYSTEM_PROMPT 的三条条款。"""

    REQUIRED = ("决策-权衡写法", "量化梯度", "中文求职雷区自查")

    def test_server_terms_present(self):
        for term in self.REQUIRED:
            self.assertIn(term, P.RESUME_OPTIMIZE_SYSTEM,
                          f"提示词缺服务端条款：{term}")

    def test_server_file_still_has_them(self):
        """反向：确认这些条款确实来自服务端，而不是我们自创的。"""
        with open(_SERVER_PROMPTS, encoding="utf-8") as fh:
            server = fh.read()
        for term in self.REQUIRED:
            self.assertIn(term, server, f"服务端提示词里没有 {term}，同步口径已漂移")

    def test_guards_attached(self):
        """优化产物是简历正文 ⇒ 必须挂防注入 + 占位符 + 禁给分三条。

        直接引用守卫常量本身（而不是手抄里面的某句话）：条款改了文案这里不会假红，
        但**漏挂**任何一条都会立刻被抓到。
        """
        for text in (P.RESUME_OPTIMIZE_SYSTEM, P.CONDENSE_SYSTEM):
            for guard in (P._INJECTION_GUARD, P._PLACEHOLDER_RULE, P._NO_SCORE_RULE):
                self.assertIn(guard.strip(), text)


class TestLocalVersionChain(_StoreCase):
    """本地新建的版本必须能出现在「历史版本」里，并且能回滚。

    不做这件事的表现：刚点完「优化」/「生成」，切到历史版本一片空白（云端 404）；
    点「恢复到此版本」直接报错。
    """

    def _seed(self):
        parent = {"id": "p1", "version": 1, "document_id": "p1", "raw_text": "V1 原文",
                  "source_file": "a.docx", "parsed_json": {"kind": "upload"},
                  "created_at": "2026-10-01T10:00:00"}
        opt = {"id": "o2", "version": 2, "document_id": "p1", "parent_id": "p1",
               "raw_text": "V2 优化稿", "source_file": "a.docx",
               "parsed_json": {"kind": "optimized"}, "created_at": "2026-10-01T11:00:00"}
        local_store.save("resumes", "create", "p1", parent)
        local_store.save("resumes", "create", "o2", opt)

    def test_cloud_down_still_lists_local_chain(self):
        self._seed()
        with mock.patch.object(AC, "_request", side_effect=AC.ApiError("404")):
            rows = AC.list_resume_versions("o2")
        self.assertEqual([r["id"] for r in rows], ["o2", "p1"], "版本号必须倒序")
        self.assertTrue(rows[0]["_local_only"])

    def test_other_document_not_mixed_in(self):
        self._seed()
        local_store.save("resumes", "create", "x9",
                         {"id": "x9", "version": 1, "document_id": "x9",
                          "raw_text": "另一份简历", "created_at": "2026-10-02T10:00:00"})
        with mock.patch.object(AC, "_request", side_effect=AC.ApiError("404")):
            rows = AC.list_resume_versions("o2")
        self.assertNotIn("x9", [r["id"] for r in rows], "把不相干的简历串进了版本历史")

    def test_rollback_appends_new_highest_version(self):
        """回滚 = **追加**一条最高版本，历史不清空（与服务端语义一致）。"""
        self._seed()
        with mock.patch.object(AC, "_request", side_effect=AssertionError("不该联网")):
            out = AC.rollback_resume_version("o2", "p1")
        self.assertEqual(out["version"], 3)
        rows = {r["id"]: r for r in local_store.list_items("resumes")}
        self.assertIn("p1", rows, "回滚把历史版本删掉了")
        self.assertIn("o2", rows, "回滚把当前版本删掉了")
        new = rows[out["id"]]
        self.assertEqual(new["raw_text"], "V1 原文", "回滚后内容不是目标版本")
        self.assertEqual(new["parsed_json"]["rolled_back_from"], 1)

    def test_rollback_rejects_cross_document(self):
        self._seed()
        local_store.save("resumes", "create", "y9",
                         {"id": "y9", "version": 1, "document_id": "y9",
                          "raw_text": "另一份", "created_at": "2026-10-02T10:00:00"})
        with mock.patch.object(AC, "_request", side_effect=AssertionError("不该联网")):
            with self.assertRaises(AC.ApiError):
                AC.rollback_resume_version("o2", "y9")

    def test_parent_cycle_does_not_hang(self):
        """异常写入造成的 parent 环必须就地断开（否则历史版本 Tab 直接卡死）。"""
        by_id = {
            "a": {"id": "a", "parent_id": "b", "version": 1},
            "b": {"id": "b", "parent_id": "a", "version": 2},
        }
        self.assertEqual(AC._root_document(by_id["a"], by_id, {}), "a")
        self.assertEqual(AC._root_document(by_id["b"], by_id, {}), "a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
