# -*- coding: utf-8 -*-
"""面试准备 / 薪资谈判的 GitHub 资产落地判据（2026-10-10）。

覆盖四项（资产来源见各用例 docstring）：
- O1 内置面试题库兜底（`data/interview_bank.py` + `interview._builtin_bank_reference`）
      ← AIGC-Interview-Book（37 专题高频考点）
- O2 面试评估 4 维度结构化（`prompts.MOCK_EVALUATOR_SYSTEM` / `MOCK_SUMMARY_SYSTEM`）
      ← ai-mock-interview / interview-copilot 的 rubric 思路
- S1 市场定位四档（`salary_benchmark.locate_offer`）
      ← Salary-Negotiation-Skill 的 `get_market_comparison`
- S2 谈判打法库注入（`data/negotiation_playbook.py` + `salary_negotiation.coach_answer`）
      ← Salary-Negotiation-Skill 的 4 策略 + 5 高压场景

全部 hermetic：不联网、不读用户数据、不调真实模型（chat 一律注入 fake）。
"""

import os
import sys
import unittest
from unittest import mock

# ⚠️ 必须做：`run_all.py` 是**直接执行** `python tests/xxx.py`，此时 sys.path[0] 是
# `tests/` 而不是仓库根，`import offerflow_desktop` 会直接 ImportError。
# （用 `-m unittest tests.xxx` 跑却正常 —— 差别就在这，别只用后者验证。）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core import salary_benchmark as sb  # noqa: E402
from offerflow_desktop.data.negotiation_playbook import (  # noqa: E402
    NEGOTIATION_PRESSURE_SCENARIOS,
    NEGOTIATION_STRATEGIES,
    build_coach_playbook,
)
from offerflow_desktop.services import interview as iv  # noqa: E402
from offerflow_desktop.services import prompts as pr  # noqa: E402
from offerflow_desktop.services import salary_negotiation as sn  # noqa: E402


class TestBuiltinInterviewBank(unittest.TestCase):
    """O1：内置题库的形状与兜底匹配。"""

    def test_bank_shape(self):
        from offerflow_desktop.data.interview_bank import INTERVIEW_BANK
        self.assertGreaterEqual(len(INTERVIEW_BANK), 20, "内置题库专题数过少")
        for t in INTERVIEW_BANK:
            self.assertTrue(t.get("topic"), "专题缺 topic")
            self.assertTrue(t.get("questions"), "专题缺 questions")

    def test_rag_jd_hits_rag_topic(self):
        out = iv._builtin_bank_reference(
            "负责 RAG 检索增强生成系统的开发，熟悉向量数据库与 LangChain")
        self.assertIn("RAG", out)
        self.assertGreater(len(out), 50)

    def test_irrelevant_jd_returns_empty(self):
        # 不能硬凑：跟 AI 无关的 JD 应返回空，而不是随便塞一个专题
        self.assertEqual(iv._builtin_bank_reference("负责门店销售和客户接待"), "")

    def test_long_irrelevant_text_returns_empty(self):
        """★ 关键回归：**长文本**无关输入也必须返回空。

        为什么单独加这条：旧版用「score>0 即入选」，而长文本经 2~4 字滑窗能产出
        上百个 token，必然在题库正文里凑出若干 2-gram 命中 ⇒ 硬凑出一个 LLM 专题。
        短 query（上一条）测不出这个 —— 那是**弱判据**。
        """
        long_unrelated = ("完全无关的查询：今天晚饭吃红烧肉还是清蒸鱼，天气怎么样，"
                          "体育新闻有什么看点，周末去哪个公园散步比较好")
        self.assertGreater(len(iv._bank_tokens(long_unrelated)), 60,
                           "这条用例的前提是 query 足够长（token 够多），否则测不出问题")
        self.assertEqual(iv._builtin_bank_reference(long_unrelated), "")

    def test_topic_name_hit_is_required(self):
        """正面判据：专题名命中才入选 —— 证明闸门不是「一律返回空」。"""
        self.assertIn("RAG", iv._builtin_bank_reference("熟悉 RAG 与向量数据库"))

    def test_empty_query_returns_empty(self):
        self.assertEqual(iv._builtin_bank_reference(""), "")

    def test_local_empty_falls_back_to_builtin_bank(self):
        """本地语料为空（新用户零面经）时必须回退内置题库，否则「参考真实考点」形同虚设。"""
        patcher = None
        try:
            patcher = mock.patch(
                "offerflow_desktop.local_ai.retrieve.retrieve_text", return_value="")
            with patcher:
                out = iv._load_interview_reference("", "熟悉 RAG 与向量数据库")
        except (ImportError, AttributeError, ModuleNotFoundError):
            # local_ai 不可导入时，函数会走 except 分支后同样回退内置题库
            out = iv._load_interview_reference("", "熟悉 RAG 与向量数据库")
        self.assertIn("RAG", out)

    def test_local_hit_wins_over_builtin(self):
        """本地有面经语料时必须优先用它，内置题库只是兜底、不能顶掉真实语料。"""
        try:
            with mock.patch("offerflow_desktop.local_ai.retrieve.retrieve_text",
                            return_value="【本地面经】某公司一面真题占位"):
                out = iv._load_interview_reference("", "熟悉 RAG")
        except (ImportError, AttributeError, ModuleNotFoundError):
            self.skipTest("local_ai.retrieve 不可导入")
        self.assertIn("本地面经", out)


class TestStructuredEvaluation(unittest.TestCase):
    """O2：面试评估从「泛评」升级为 4 维度评分。"""

    DIMS = ("技术深度", "结构完整", "量化程度", "可信度")

    def test_evaluator_has_four_dimensions(self):
        # 断言「维度定义行」而非「字符串出现过」：评估提示词的评分格式行里也会出现
        # 维度名（「评分：… 可信度x」），只断言裸字符串时，把维度定义整行删掉
        # 也照样 PASS —— 反向对照 M6 暴露的弱判据。
        for dim in self.DIMS:
            self.assertIn(f"- {dim}：", pr.MOCK_EVALUATOR_SYSTEM)
        self.assertIn("1~5", pr.MOCK_EVALUATOR_SYSTEM)

    def test_evaluator_dropped_thirty_char_limit(self):
        # 旧版是「改进建议（30字以内）」的泛评，升级后不应再出现这种字数硬限
        self.assertNotIn("30字以内", pr.MOCK_EVALUATOR_SYSTEM)
        self.assertNotIn("20字以内", pr.MOCK_EVALUATOR_SYSTEM)

    def test_summary_has_dimension_rollup(self):
        self.assertIn("维度汇总", pr.MOCK_SUMMARY_SYSTEM)
        for dim in self.DIMS:
            self.assertIn(dim, pr.MOCK_SUMMARY_SYSTEM)

    def test_evaluate_answer_prompt_asks_four_dims(self):
        captured = {}

        def fake(system, user):
            captured["system"] = system
            captured["user"] = user
            return "ok"

        iv.evaluate_answer("问题", "回答", chat_fn=fake)
        for dim in self.DIMS:
            self.assertIn(dim, captured["user"])


class TestMarketPosition(unittest.TestCase):
    """S1：具体 offer 的市场定位四档（对齐 get_market_comparison）。"""

    def test_four_bands(self):
        self.assertEqual(sb.locate_offer(10000, 16000, 8000)["position"], "below_market")
        self.assertEqual(sb.locate_offer(10000, 16000, 9500)["position"], "low_end")
        self.assertEqual(sb.locate_offer(10000, 16000, 13000)["position"], "market_rate")
        self.assertEqual(sb.locate_offer(10000, 16000, 18000)["position"], "above_market")

    def test_invalid_numbers_are_unknown_not_guessed(self):
        self.assertEqual(sb.locate_offer(10000, 16000, 0)["position"], "unknown")

    def test_gap_to_high_quantifies_room(self):
        self.assertEqual(sb.locate_offer(10000, 16000, 13000)["gap_to_high"], 3000)
        self.assertEqual(sb.locate_offer(10000, 16000, 18000)["gap_to_high"], -2000)

    def test_caliber_note_is_runtime_string_not_a_comment(self):
        """口径说明必须是 DATA_SOURCES 里的**运行时字符串**。

        2026-10-10 踩坑：口径说明（全市场分布 ≠ 同一岗位内微调）只写在 `#:` 注释里
        —— 人看得到，但注释**不进产物**（co_consts）⇒ 拆包判据 `has_text("全市场")`
        恒 FAIL。这是**判据空靶**，不是代码缺失。真源必须是运行时字符串，
        同时也能随 `sources` 一起回给前端当「定薪依据」的注脚。
        """
        blob = "\n".join(sb.DATA_SOURCES)
        self.assertIn("全市场", blob)
        self.assertIn("同一岗位内", blob)
        self.assertIn("不可互相换算", blob)


class TestNegotiationPlaybook(unittest.TestCase):
    """S2：谈判打法库 + HR/教练提示词增强。"""

    def test_playbook_covers_all_strategies(self):
        text = build_coach_playbook()
        for s in NEGOTIATION_STRATEGIES:
            self.assertIn(s["name"], text)

    def test_playbook_covers_five_pressure_scenarios(self):
        self.assertEqual(len(NEGOTIATION_PRESSURE_SCENARIOS), 5)
        text = build_coach_playbook()
        for sc in NEGOTIATION_PRESSURE_SCENARIOS:
            self.assertIn(sc["name"], text)

    def test_hr_prompt_has_new_pressure_tactics(self):
        for kw in ("职级压制", "锚定试探", "拆包转移", "时间压力"):
            self.assertIn(kw, pr.NEGOTIATION_HR_SYSTEM)

    def test_coach_prompt_asks_five_dimensions(self):
        for kw in ("锚定", "让步节奏", "框架接受", "价值陈述", "底线与退路"):
            self.assertIn(kw, pr.NEGOTIATION_COACH_SYSTEM)

    def test_coach_gets_playbook_injected(self):
        captured = {}

        def fake(system, user):
            captured["system"] = system
            return "ok"

        sn.coach_answer("HR 说的话", "我的回应", "14000", "12000", "业绩点", chat_fn=fake)
        self.assertIn("打法对照", captured["system"])
        self.assertIn("替代补偿", captured["system"])


if __name__ == "__main__":
    unittest.main()
