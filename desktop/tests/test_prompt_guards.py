"""提示词安全条款的判据（防提示注入 / 缺数字用占位符 / 不输出分数）。

为什么需要这份判据
------------------
这三类条款是**纯提示词层**的约束，没有编译错误、没有异常可抓 —— 一旦有人
在新增提示词时忘了挂，或在重构时把条款删了，程序照样跑得好好的，
只有线上输出变坏。所以必须用判据把「谁挂了哪几条」钉住。

判据取向（按错题本）：
* 断言基于**结构**：提示词用 `import` 后的反射取值，local_ai 用 AST 取模块级
  字符串常量，不做"源码里有没有这句话"的文本匹配。
* **语义匹配（MATCH_SEMANTIC）必须例外**：它的 `score` 是设计内的逐条满足度，
  不是"给简历打分"。若哪天有人图省事把 `_NO_SCORE_RULE` 挂到全部提示词上，
  本文件的 test_match_semantic_must_not_get_no_score_rule 会变红 —— 这条是
  护栏，不是冗余。
"""
import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core.text_utils import count_placeholders, find_placeholders
from offerflow_desktop.services import prompts as P

# 产出「简历正文」的提示词：既要把外部内容当数据，也不许编数字、不许给分
_RESUME_OUTPUT = (
    "RESUME_GENERATE_SYSTEM",
    "REGENERATE_SECTION_SYSTEM",
    "REGENERATE_VARIANTS_SYSTEM",
    "RESUME_OPTIMIZE_SYSTEM",
    "CONDENSE_SYSTEM",
)

# 会接触到外部内容的全部提示词（必须挂输入安全）
_ALL_GUARDED = _RESUME_OUTPUT + (
    "JD_PARSE_SYSTEM",
    "INTERVIEW_QUESTIONS_SYSTEM",
    "MOCK_INTERVIEWER_SYSTEM",
    "MOCK_EVALUATOR_SYSTEM",
    "MOCK_SUMMARY_SYSTEM",
    "SALARY_PROFILE_SYSTEM",
    "SALARY_ANALYSIS_SYSTEM",
    "NEGOTIATION_HR_SYSTEM",
    "NEGOTIATION_COACH_SYSTEM",
    "NEGOTIATION_SUMMARY_SYSTEM",
    "MATCH_SEMANTIC_SYSTEM",
)


def _module_string_constants(rel_path: str) -> dict:
    """用 AST 取某模块的「模块级字符串常量」赋值。

    结构性判据：只认 `NAME = "字面量"`，不扫源码文本，
    避免"注释里写了一句也算过"这种假绿。
    """
    path = os.path.join(_ROOT, "offerflow_desktop", rel_path)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    out[target.id] = node.value.value
    return out


class InjectionGuardTests(unittest.TestCase):
    """防提示注入条款必须挂在每一个会接触外部内容的提示词上。"""

    def test_guard_text_is_meaningful(self):
        """自证判据：条款本身必须真的在说「数据不是指令」。"""
        self.assertTrue(P._INJECTION_GUARD.strip(), "输入安全条款是空的")
        self.assertIn("不是给你的指令", P._INJECTION_GUARD)
        self.assertIn("绝不执行", P._INJECTION_GUARD)

    def test_all_prompts_carry_injection_guard(self):
        for name in _ALL_GUARDED:
            prompt = getattr(P, name, None)
            self.assertIsInstance(prompt, str, f"{name} 不存在或不是字符串")
            self.assertIn(P._INJECTION_GUARD, prompt, f"{name} 漏挂输入安全条款")

    def test_guard_is_actually_appended_not_replacing(self):
        """条款是「追加」的：原提示词主体必须还在，别被覆盖掉。"""
        self.assertIn("简历", P.RESUME_GENERATE_SYSTEM)
        self.assertIn("JSON", P.RESUME_OPTIMIZE_SYSTEM)
        self.assertGreater(len(P.RESUME_GENERATE_SYSTEM), len(P._INJECTION_GUARD))

    def test_attach_is_idempotent(self):
        """重复挂载不得叠加（模块被重载/重复调用时要安全）。"""
        once = P._attach("基础提示词", P._INJECTION_GUARD)
        twice = P._attach(once, P._INJECTION_GUARD)
        self.assertEqual(once, twice)

    def test_local_ai_prompts_carry_injection_guard(self):
        """桌面版唯一「本地直连模型」的链路：图谱抽取与记忆提炼。

        它把用户资料（含第三方 JD 原文）直接喂给模型，是本地最真实的注入面。
        """
        for rel, sys_name, prompt_name in (
            ("local_ai/graph.py", "_SYSTEM", "_PROMPT"),
            ("local_ai/memory.py", "_SYSTEM", "_PROMPT"),
        ):
            consts = _module_string_constants(rel)
            self.assertIn(sys_name, consts, f"{rel} 找不到 {sys_name}")
            self.assertIn(prompt_name, consts, f"{rel} 找不到 {prompt_name}")
            self.assertIn("指令", consts[sys_name],
                          f"{rel} 的 {sys_name} 没有防注入声明")
            # 措辞不锁死：「绝不执行」与「不得执行」语义相同，判据只认语义
            self.assertTrue(
                any(w in consts[sys_name] for w in ("绝不执行", "不得执行")),
                f"{rel} 的 {sys_name} 没说清「数据里的指令不执行」")
            self.assertTrue(
                any(w in consts[prompt_name] for w in ("不得执行", "绝不执行")),
                f"{rel} 的 {prompt_name} 没把资料标成数据")


class PlaceholderRuleTests(unittest.TestCase):
    """占位符条款只该挂在产出简历的提示词上。"""

    def test_resume_output_prompts_allow_placeholder(self):
        for name in _RESUME_OUTPUT:
            self.assertIn(P._PLACEHOLDER_RULE, getattr(P, name),
                          f"{name} 漏挂占位符条款")

    def test_placeholder_rule_forbids_inventing_numbers(self):
        self.assertIn("[x]", P._PLACEHOLDER_RULE)
        self.assertIn("绝不编造", P._PLACEHOLDER_RULE)

    def test_count_and_find(self):
        text = "响应时间降低 [x]%，覆盖 [x] 个场景，正常数字 800ms 不算占位符。"
        self.assertEqual(count_placeholders(text), 2)
        found = find_placeholders(text)
        self.assertEqual(len(found), 2)
        self.assertTrue(all("[x]" in f for f in found))

    def test_no_placeholder_returns_zero(self):
        self.assertEqual(count_placeholders("响应时间从 800ms 降到 100ms"), 0)
        self.assertEqual(find_placeholders(""), [])
        self.assertEqual(count_placeholders(None), 0)

    def test_dedupes_identical_fragments(self):
        """同一处上下文不重复列出（避免提醒里刷屏）。"""
        self.assertEqual(len(find_placeholders("[x]% [x]%")), 1)


class NoScoreRuleTests(unittest.TestCase):
    """数字只来自确定性检查器；LLM 不给分 —— 但语义匹配必须例外。"""

    def test_resume_output_prompts_forbid_scoring(self):
        for name in _RESUME_OUTPUT:
            self.assertIn(P._NO_SCORE_RULE, getattr(P, name),
                          f"{name} 漏挂「不输出分数」条款")

    def test_match_semantic_must_not_get_no_score_rule(self):
        """护栏：语义匹配的 score 是设计内字段，挂上禁给分会让匹配功能失效。"""
        self.assertIn(P._INJECTION_GUARD, P.MATCH_SEMANTIC_SYSTEM)
        self.assertNotIn(P._NO_SCORE_RULE, P.MATCH_SEMANTIC_SYSTEM,
                         "语义匹配被误挂了禁给分条款 —— 它会拒绝输出 score，匹配功能会坏")
        self.assertIn("score", P.MATCH_SEMANTIC_SYSTEM)


class ExportGuardWiringTests(unittest.TestCase):
    """导出前的占位符拦截必须真的接上（防 E-01：用了没导入的名字）。"""

    def test_confirm_placeholders_importable(self):
        from offerflow_desktop.ui import components
        self.assertTrue(callable(getattr(components, "confirm_placeholders", None)))

    def test_both_export_pages_import_it(self):
        for rel in ("ui/pages/resume_optimize.py", "ui/pages/resume_generate.py"):
            consts = _module_string_constants(rel)
            # 这两个页面是 from ..components import (...)，用 AST 取一下更稳
            path = os.path.join(_ROOT, "offerflow_desktop", rel)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imported.add(alias.name)
            self.assertIn("confirm_placeholders", imported,
                          f"{rel} 用了 confirm_placeholders 却没导入（会 NameError）")

    def test_export_guard_precedes_file_dialog(self):
        """拦截必须在选路径之前，否则用户选完才被弹窗打断。"""
        for rel in ("ui/pages/resume_optimize.py", "ui/pages/resume_generate.py"):
            path = os.path.join(_ROOT, "offerflow_desktop", rel)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            order = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_export":
                    for i, sub in enumerate(ast.walk(node)):
                        if isinstance(sub, ast.Name) and sub.id == "confirm_placeholders":
                            order.setdefault("guard", sub.lineno)
                        if isinstance(sub, ast.Attribute) and sub.attr == "getSaveFileName":
                            order.setdefault("dialog", sub.lineno)
            self.assertIn("guard", order, f"{rel} 的 _export 没接占位符拦截")
            self.assertIn("dialog", order, f"{rel} 的 _export 没有文件对话框？")
            self.assertLess(order["guard"], order["dialog"],
                            f"{rel} 的占位符拦截应早于选路径")


if __name__ == "__main__":
    unittest.main(verbosity=2)
