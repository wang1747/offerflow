#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2：本地生成主流程（`services/resume_generation.generate_resume`）的护栏验证。

这一段是「生成在本机跑」的核心 —— 把服务端 ``resume_generation/services.py``
的 8 项护栏搬到桌面（同步版）。测试分三层：

1. **行为层**（最重要）：用假模型喂**故意编造**的 payload，断言产物被护栏纠正 ——
   - 事实回填：学校 / 公司 / 姓名 / 联系方式一律用用户填的，模型编的一律丢掉；
   - 数字兜底：模型编的数字必须被删除并留痕；
   - 编造剔除：用户没写的技能 / 证书必须被摘掉，且**渲染文本同步变短**；
   - 一页 fail-closed：精简不出来时报错拒落库，而不是放行「界面一页、导出两页」。
2. **同源层**：桌面 `RESUME_GENERATE_SYSTEM` 必须含服务端最新 `SYSTEM_PROMPT` 的
   关键条款（决策-权衡 / 中文求职雷区 / 动态字数 / risks / jd_alignment）——
   改前这些一条都没有。
3. **动态层**：字数目标必须由 `core.sufficiency` 决定，不能是硬编码的 700。

⚠️ 反向对照：`tests/_mutate_ai_generate_check.py` 会逐个砍掉护栏并验证本文件变红。

运行：python tests/test_ai_local_generate.py [TestCase.test_method]
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

from offerflow_desktop.services import resume_generation as RG   # noqa: E402
from offerflow_desktop.services import resume_optimizer as RO    # noqa: E402
from offerflow_desktop.services import semantic_score as SS      # noqa: E402
from offerflow_desktop.services import prompts as P              # noqa: E402

# monorepo：后端源码在 ../web/backend（desktop/ 与 web/ 同级）
_SERVER_PROMPTS = os.path.join(
    os.path.dirname(_ROOT), "web", "backend", "app", "modules",
    "resume_generation", "prompts.py")


# ============ 构造 ============

def _req(**over):
    """用户填写的原始草稿（事实真源）。"""
    base = {
        "name": "张三",
        "position": "后端开发工程师",
        "phone": "13800000000",
        "email": "zhang@example.com",
        "github": "",
        "direction": "tech",
        "education": [{
            "school": "某某大学", "major": "计算机科学与技术", "degree": "本科",
            "start": "2023.09", "end": "2027.06",
            "courses": "", "gpa": "", "honors": "", "detail": "",
        }],
        "experiences": [{
            "type": "project", "name": "OfferFlow", "role": "后端开发",
            "start": "2025.03", "end": "2026.06",
            "bullets": ["主导简历生成链路重构，引入事实保真校验"],
        }],
        "skills": ["Python", "FastAPI", "Redis"],
        "certifications": [],
        "summary": "后端开发方向，主导过简历生成链路的保真改造",
        "jd_text": "招聘后端开发工程师，要求熟悉 Python、FastAPI、Redis",
    }
    base.update(over)
    return base


def _payload(**over):
    """模型返回的 payload —— **故意全用编造值**，护栏若失效就会漏进产物。"""
    base = {
        "name": "模型编造姓名",
        "position": "后端开发工程师",
        "contact": {"phone": "19900000000", "email": "fake@example.com",
                    "github": "github.com/fake"},
        "summary": "主导订单系统性能优化，支撑核心链路稳定运行",
        "education": [{"school": "编造大学", "major": "编造专业", "degree": "硕士",
                       "start": "2019.09", "end": "2022.06", "detail": ""}],
        "experiences": [{
            "type": "project", "name": "编造公司", "role": "编造角色",
            "start": "2018.01", "end": "2019.01",
            "bullets": ["主导订单系统重构，引入缓存与异步化，支撑核心链路稳定运行"],
        }],
        "skills": ["Python", "FastAPI"],
        "certifications": [],
        "tips": [],
    }
    base.update(over)
    return base


def _run(req, payload, condense=None, semantic=None):
    """跑一遍本地生成，所有 LLM 出口都换成假模型（hermetic）。"""
    payload_json = json.dumps(payload, ensure_ascii=False)
    condense_json = json.dumps(condense or {}, ensure_ascii=False)
    semantic_json = json.dumps(semantic or {}, ensure_ascii=False)
    with mock.patch.object(RG, "chat", return_value=payload_json), \
         mock.patch.object(RO, "chat", return_value=condense_json), \
         mock.patch.object(SS, "_default_chat", return_value=semantic_json), \
         mock.patch.object(RG, "_load_reference",
                              return_value={"text": "", "trace": {}}):
        return RG.generate_resume(req)


# ============ 1. 事实回填（最关键：防编造学校 / 公司 / 联系方式） ============

class TestFactBackfill(unittest.TestCase):
    def test_identity_uses_user_input(self):
        r = _run(_req(), _payload())
        self.assertNotIn("error", r, r.get("error"))
        st = r["structured"]
        # 姓名 / 联系方式是硬事实 —— 用户填了就用用户的
        self.assertEqual(st["name"], "张三")
        self.assertEqual(st["contact"]["phone"], "13800000000")
        self.assertEqual(st["contact"]["email"], "zhang@example.com")
        # 模型编的 github 必须被清掉（用户没填）。剔除后该键整个消失是预期行为
        # （与 `_strip_fabricated_facts` 的 dict 推导一致）—— 用 get 断言而不是下标。
        self.assertEqual(st["contact"].get("github", ""), "")

    def test_education_uses_user_input(self):
        r = _run(_req(), _payload())
        edu = r["structured"]["education"]
        self.assertEqual(len(edu), 1)
        self.assertEqual(edu[0]["school"], "某某大学")
        self.assertEqual(edu[0]["major"], "计算机科学与技术")
        self.assertEqual(edu[0]["degree"], "本科")

    def test_experience_facts_use_user_input(self):
        r = _run(_req(), _payload())
        exp = r["structured"]["experiences"][0]
        self.assertEqual(exp["name"], "OfferFlow")
        self.assertEqual(exp["role"], "后端开发")
        self.assertEqual(exp["start"], "2025.03")
        self.assertEqual(exp["end"], "2026.06")

    def test_bullets_still_come_from_model(self):
        """回填只作用于事实字段 —— bullets 必须仍是模型写的（表达层要优化）。"""
        r = _run(_req(), _payload())
        exp = r["structured"]["experiences"][0]
        self.assertTrue(any("订单系统重构" in b for b in exp["bullets"]), exp["bullets"])

    def test_experience_count_mismatch_errors(self):
        """模型少回经历时必须报错 —— 静默丢弃会把用户的整段经历弄没。"""
        p = _payload()
        p["experiences"] = []
        r = _run(_req(), p)
        self.assertIn("error", r)
        self.assertIn("经历", r["error"])

    def test_fidelity_catches_fabrication_without_backfill(self):
        """反向对照锚点：**不传回填参数**时，模型编造的学校/公司必须被 fidelity 检出。

        这证明「原样回填」不是装饰 —— 它挡的是真问题。若把回填去掉而保真层
        又只查技能/证书，编造的学校名就会一路进库（这正是本轮之前的桌面状态）。
        """
        r = RG.build_result(_payload(), "后端开发工程师", "招聘后端工程师")
        st = r["structured"]
        self.assertEqual(st["education"][0]["school"], "编造大学")   # 没回填 → 编造值原样留下
        fid = RG.check_generation_fidelity(_req(), st)
        self.assertFalse(fid["passed"])
        self.assertEqual(fid["added_schools"], ["编造大学"])
        self.assertEqual(fid["added_companies"], ["编造公司"])


# ============ 2. 编造技能 / 证书就地剔除 ============

class TestFabricatedFactsStripped(unittest.TestCase):
    def test_added_skill_is_removed(self):
        r = _run(_req(), _payload(skills=["Python", "FastAPI", "Kubernetes", "Golang"]))
        st = r["structured"]
        self.assertNotIn("Kubernetes", st["skills"])
        self.assertNotIn("Golang", st["skills"])
        self.assertIn("Python", st["skills"])
        removed = (r.get("fidelity") or {}).get("removed") or {}
        self.assertIn("Kubernetes", removed.get("skills") or [])
        self.assertTrue(any("已删除原始信息里没有的内容" in t for t in r.get("tips") or []))

    def test_added_cert_is_removed(self):
        r = _run(_req(), _payload(certifications=["PMP", "CPA"]))
        self.assertEqual(r["structured"]["certifications"], [])
        removed = (r.get("fidelity") or {}).get("removed") or {}
        self.assertEqual(sorted(removed.get("certifications") or []), ["CPA", "PMP"])

    def test_removed_skill_also_leaves_rendered_text(self):
        """structured 是唯一真源：删技能后渲染文本必须同步变短（否则导出与界面不一致）。"""
        r = _run(_req(), _payload(skills=["Python", "FastAPI", "Kubernetes"]))
        self.assertNotIn("Kubernetes", r["resume_text"])

    def test_skill_groups_pruned_too(self):
        """桌面渲染优先走 skill_groups —— 只删平铺 skills 不够，必须同步清理分组。"""
        r = _run(_req(), _payload(
            skills=["Python", "FastAPI", "Kubernetes"],
            skill_groups={"语言": ["Python"], "工具与平台": ["Kubernetes"]},
        ))
        groups = r["structured"].get("skill_groups") or {}
        self.assertNotIn("Kubernetes", json.dumps(groups, ensure_ascii=False))
        self.assertNotIn("Kubernetes", r["resume_text"])


# ============ 3. 数字确定性兜底 ============

class TestNumberSanitize(unittest.TestCase):
    def test_fabricated_percent_is_stripped(self):
        p = _payload()
        p["experiences"][0]["bullets"] = [
            "主导订单系统重构，接口响应时间降低 87%，支撑日均 300000 次调用"]
        r = _run(_req(), p)
        text = r["resume_text"]
        self.assertNotIn("87%", text)
        self.assertNotIn("300000", text)
        self.assertTrue(any("已按事实保真删除" in t for t in r.get("tips") or []), r.get("tips"))

    def test_user_number_is_kept(self):
        """用户写过的数字不能被误删 —— 否则兜底过头会丢真实成果。"""
        req = _req()
        req["experiences"][0]["bullets"] = ["主导订单系统重构，接口响应时间降低 40%"]
        p = _payload()
        p["experiences"][0]["bullets"] = ["主导订单系统重构，接口响应时间降低 40%"]
        r = _run(req, p)
        self.assertIn("40%", r["resume_text"])

    def test_clean_text_untouched(self):
        r = _run(_req(), _payload())
        self.assertFalse(any("已按事实保真删除" in t for t in r.get("tips") or []))


# ============ 4. 一页硬约束 fail-closed ============

class TestOnePageFailClosed(unittest.TestCase):
    def _long_bullets(self):
        # 每条**不同**、约 60 字、不含数字 —— 不触发数字净化，专测一页约束
        tpl = ("主导订单系统{}模块的重构与性能优化工作，深入梳理链路瓶颈并推动跨团队相关方"
               "协同改造，最终保障核心链路在高并发场景下稳定运行")
        return [tpl.format(ch) for ch in "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"]

    def _long_payload(self):
        p = _payload()
        p["experiences"][0]["bullets"] = self._long_bullets()
        return p

    def test_oversize_without_condense_fails_closed(self):
        # 精简模型返回空（拿不出 optimized_text）→ 必须报错拒落库，而不是放行
        r = _run(_req(), self._long_payload(), condense={})
        self.assertIn("error", r)
        self.assertIn("一页", r["error"])

    def test_oversize_accepts_real_condense(self):
        bullets = self._long_bullets()
        head = "张三\n项目经历\nOfferFlow（后端开发）｜2025.03 – 2026.06"
        short = head + "\n- " + bullets[0] + "\n- " + bullets[1]
        r = _run(_req(), self._long_payload(),
                 condense={"optimized_text": short, "removed": ["无关铺垫"]})
        self.assertNotIn("error", r, r.get("error"))
        self.assertLess(len(r["resume_text"]), 1400)
        self.assertLessEqual(len(r["structured"]["experiences"][0]["bullets"]), 2)

    def test_normal_size_passes(self):
        r = _run(_req(), _payload())
        self.assertNotIn("error", r)


# ============ 5. 动态字数（不能硬编码 700） ============

class TestDynamicLength(unittest.TestCase):
    def test_empty_input_gets_smaller_target(self):
        # 必须保留 1 条经历 —— 与 _payload 的条数一致（否则 build_result 报「丢失经历」）
        req = _req(education=[], skills=[], certifications=[], summary="",
                   jd_text="", phone="", email="")
        r = _run(req, _payload())
        suf = r.get("sufficiency") or {}
        self.assertEqual(suf.get("level"), "insufficient")
        self.assertLess(suf.get("target_length"), 800)

    def test_rich_input_gets_full_target(self):
        bullets = ["主导 XX 系统后端开发并优化性能，接口响应从 800ms 降至 100ms"]
        req = _req(
            experiences=[{"type": "project", "name": "OfferFlow", "role": "后端开发",
                          "start": "2025.03", "end": "2026.06", "bullets": bullets * 3}],
            certifications=["CET-6"], jd_text="招聘后端工程师",
        )
        r = _run(req, _payload())
        suf = r.get("sufficiency") or {}
        self.assertEqual(suf.get("level"), "sufficient")
        self.assertEqual(suf.get("target_length"), 800)

    def test_target_length_reaches_user_prompt(self):
        """字数目标必须真的进了 prompt —— 否则动态判据等于没接上。

        判据引用 ``assess_input_sufficiency`` 的**真值**，不抄字面量。
        """
        from offerflow_desktop.core import sufficiency as SUF
        req = _req(education=[], skills=[], certifications=[], summary="", jd_text="")
        target = SUF.assess_input_sufficiency(req)["target_length"]
        seen = {}

        def _fake_chat(system, user, **kw):
            seen["user"] = user
            return json.dumps(_payload(), ensure_ascii=False)

        with mock.patch.object(RG, "chat", side_effect=_fake_chat), \
             mock.patch.object(RO, "chat", return_value="{}"), \
             mock.patch.object(SS, "_default_chat", return_value="{}"), \
             mock.patch.object(RG, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            RG.generate_resume(req)
        self.assertIn("【篇幅要求】", seen["user"])
        self.assertIn(str(target), seen["user"])


# ============ 6. 语义评分接入 ============

class TestSemanticGate(unittest.TestCase):
    def test_semantic_scores_attached(self):
        r = _run(_req(), _payload(), semantic={
            "result_credibility": {"score": 88, "reason": "成果可信"},
            "expression_quality": {"score": 90, "reason": "表达专业"},
            "jd_fit": {"score": 86, "reason": "契合岗位"},
            "overall": "整体不错",
        })
        sem = r.get("semantic") or {}
        self.assertEqual(sem.get("total_score"), 88)
        self.assertEqual(sem.get("jd_fit", {}).get("score"), 86)

    def test_semantic_failure_does_not_break_flow(self):
        r = _run(_req(), _payload(), semantic=None)
        self.assertNotIn("error", r)
        self.assertEqual(r.get("semantic"), {})


# ============ 7. 提示词同源（服务端最新条款必须在） ============

class TestPromptParity(unittest.TestCase):
    # 这些是服务端 `SYSTEM_PROMPT`（2026-10 版）独有、桌面旧版**一条都没有**的条款
    _SERVER_CLAUSES = [
        "决策-权衡写法",          # 第 8 条：技术选型/架构权衡的写法
        "中文求职雷区",            # 服务外包 / 玩具项目 / 精通
        "服务外包",                # 雷区细节
        "从「做了什么」升级到「解决了什么问题」",   # 第 9 条
        "按用户消息中的具体字数执行",   # 动态字数（旧版写死 800）
        "jd_alignment",            # JD 对齐映射输出
        "risks",                   # 雷区提示输出
    ]

    def test_desktop_prompt_has_server_clauses(self):
        for clause in self._SERVER_CLAUSES:
            self.assertIn(clause, P.RESUME_GENERATE_SYSTEM,
                          "桌面生成提示词缺少服务端条款：%s" % clause)

    def test_desktop_prompt_keeps_own_extras(self):
        """桌面独有的三处增量也要在（否则会丢功能，不是「更旧」而是「更少」）。"""
        for clause in ("skill_groups", "【桌面增量】", "25~60 字"):
            self.assertIn(clause, P.RESUME_GENERATE_SYSTEM)

    def test_server_source_reachable_and_matches(self):
        if not os.path.exists(_SERVER_PROMPTS):
            self.skipTest("服务端源码不可达")
        with open(_SERVER_PROMPTS, encoding="utf-8") as fh:
            src = fh.read()
        # 服务端自身的条款必须与桌面断言一致（服务端改了、这里会红，提醒同步）
        for clause in self._SERVER_CLAUSES:
            self.assertIn(clause, src, "服务端提示词已变动，请复核桌面同步：%s" % clause)


# ============ 8. 质量重试闭环 + 参考来源标注 ============

class TestQualityRetry(unittest.TestCase):
    def test_quality_retry_fires_with_feedback_hint(self):
        """质量不达标必须**带着评分反馈**重写一次。

        判据按错题本：不数「chat 被调了几次」这种总量，而是数**重试分支里那次调用**
        的特征 —— 第二次 prompt 必须是第一次 + "\\n\\n" + 反馈 hint。
        """
        weak = "参与订单系统的日常维护工作，配合相关方处理线上问题，跟进需求交付进度"
        p = _payload()
        p["experiences"][0]["bullets"] = [weak] * 25     # 渲染后 ~900 字：不触发字数重试
        payload_json = json.dumps(p, ensure_ascii=False)
        calls = []

        def _fake(system, user, **kw):
            calls.append(user)
            return payload_json

        with mock.patch.object(RG, "chat", side_effect=_fake), \
             mock.patch.object(RO, "chat", return_value="{}"), \
             mock.patch.object(SS, "_default_chat", return_value="{}"), \
             mock.patch.object(RG, "_load_reference",
                              return_value={"text": "", "trace": {}}):
            r = RG.generate_resume(_req())

        self.assertFalse((r.get("quality") or {}).get("passed"))
        self.assertEqual(len(calls), 2, "质量不达标应恰好重试一次（此处不该有字数重试）")
        self.assertTrue(calls[1].startswith(calls[0]), "重试 prompt 必须是在原 prompt 上追加反馈")
        self.assertNotEqual(calls[0], calls[1])


class TestReferenceAnnotation(unittest.TestCase):
    def test_reference_source_annotated(self):
        """命中参考语料时要标注来源（对标标书 materialSources）。"""
        payload_json = json.dumps(_payload(), ensure_ascii=False)
        with mock.patch.object(RG, "chat", return_value=payload_json), \
             mock.patch.object(RO, "chat", return_value="{}"), \
             mock.patch.object(SS, "_default_chat", return_value="{}"), \
             mock.patch.object(RG, "_load_reference",
                              return_value={"text": "参考范文正文",
                                            "trace": {"corpus_hits": 1}}):
            r = RG.generate_resume(_req())
        self.assertIn("reference_source", r)

    def test_no_reference_no_annotation(self):
        r = _run(_req(), _payload())
        self.assertNotIn("reference_source", r)


# ============ 9. 落本地主存（方案 A：本地主存 + 同步） ============

class TestSaveGenerated(unittest.TestCase):
    def test_saves_into_local_store(self):
        """落点必须是 ``local_store``（UI 生成历史与 outbox 都只认它）。"""
        from offerflow_desktop import local_store
        r = _run(_req(), _payload())
        with mock.patch.object(local_store, "save", return_value=7) as spy:
            out = RG.save_generated(r)
        self.assertTrue(out.get("id"))
        self.assertEqual(out.get("version"), 1)
        args = spy.call_args[0]
        self.assertEqual(args[0], "resumes")
        self.assertEqual(args[1], "create")
        self.assertEqual(args[2], out["id"])
        item = args[3]
        self.assertEqual(item["raw_text"], out["resume_text"])
        self.assertEqual(item["parsed_json"]["structured"]["name"], "张三")
        self.assertEqual(item["parsed_json"]["generation"]["resume_text"], out["resume_text"])

    def test_roundtrip_visible_to_list_items(self):
        """端到端：落库后必须能从 ``local_store.list_items('resumes')`` 读回来 ——
        这正是 UI 生成历史的数据源（写错表就会「存了却看不到」）。"""
        from offerflow_desktop import local_store
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(local_store, "_DB_PATH", os.path.join(tmp, "t.db")), \
                 mock.patch.object(local_store, "_account", "tester"):
                out = RG.save_generated(_run(_req(), _payload()))
                items = local_store.list_items("resumes")
        self.assertTrue(out.get("id"))
        self.assertIn(out["id"], [str(i.get("id")) for i in items])

    def test_not_logged_in_does_not_break(self):
        from offerflow_desktop import local_store
        r = _run(_req(), _payload())
        with mock.patch.object(local_store, "save", return_value=None):
            out = RG.save_generated(r)
        self.assertNotIn("id", out)
        self.assertNotIn("error", out)

    def test_error_result_is_passed_through(self):
        from offerflow_desktop import local_store
        with mock.patch.object(local_store, "save", return_value=1) as spy:
            out = RG.save_generated({"error": "生成失败"})
        self.assertEqual(out["error"], "生成失败")
        self.assertFalse(spy.called)


# ============ 10. 候选变体的数字净化（服务端已修的那条零护栏路径） ============

class TestVariantSanitize(unittest.TestCase):
    def test_fabricated_number_in_candidate_is_stripped(self):
        structured = {"summary": "后端开发", "experiences": [
            {"name": "OfferFlow", "role": "后端开发", "bullets": ["主导重构"]}]}
        payload = {"candidates": [
            "主导订单系统重构，接口响应时间降低 87%，支撑日均 300000 次调用",
            "主导订单系统重构，接口响应时间明显降低",
            "主导订单系统重构，支撑核心链路稳定运行",
        ]}
        with mock.patch.object(RG, "chat",
                               return_value=json.dumps(payload, ensure_ascii=False)):
            cands = RG.regenerate_section_variants(structured, "summary", None, "")
        self.assertEqual(len(cands), 3)
        self.assertNotIn("87%", cands[0])
        self.assertNotIn("300000", cands[0])

    def test_user_number_in_candidate_is_kept(self):
        structured = {"summary": "接口响应时间降低 40%", "experiences": []}
        payload = {"candidates": ["接口响应时间降低 40%，支撑核心链路稳定运行"]}
        with mock.patch.object(RG, "chat",
                               return_value=json.dumps(payload, ensure_ascii=False)):
            cands = RG.regenerate_section_variants(structured, "summary", None, "")
        self.assertIn("40%", cands[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
