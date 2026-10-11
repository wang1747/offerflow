#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 1 移植的本地判据层：sufficiency / skill_keywords / quality / page_fit / fidelity。

这五个模块是从服务端（offerflow-server）逐字移植过来的「纯函数判据」，
它们是「生成在本机跑」的护栏底座。本文件的职责是**证明移植是对的**，分三层：

1. **行为层**：每个判据的**结论值**（分档 / passed / hard_count / gaps 文案 / 净化结果）——
   不依赖新增符号，砍掉判据就会变红。
2. **对齐层**：桌面内部必须自洽的两处，错了会**静默**失效：
   - ``page_fit.header_text`` 必须等于桌面 render 的经历标题 —— 否则块匹配退化成
     全池匹配（A 段经历认领 B 段的要点 = 数据污染）；
   - ``skill_keywords.DIRECTION_SKILLS`` 必须等于 ``JOB_TEMPLATES[*].recommendedSkills`` ——
     否则「方向高频词覆盖」这条 ATS 判据恒返回空。
3. **同源层**：服务端源码可达时，用 ast 提取判据常量逐条比对（不可达则 skip）。

⚠️ 反向对照：`tests/_mutate_ai_judges_check.py` 会逐个砍判据并验证本文件变红。

运行：python tests/test_ai_local_judges.py [TestCase.test_method]
"""

import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core import fidelity as F            # noqa: E402
from offerflow_desktop.core import page_fit as PF           # noqa: E402
from offerflow_desktop.core import quality as Q             # noqa: E402
from offerflow_desktop.core import skill_keywords as SK     # noqa: E402
from offerflow_desktop.core import sufficiency as SUF       # noqa: E402
from offerflow_desktop.services import resume_generation as RG  # noqa: E402

# 服务端源码树（monorepo 内 desktop/ 与 web/ 同级；不可达时相关用例 skip）
_SERVER = os.path.join(os.path.dirname(_ROOT), "web", "backend", "app")


def _server_assign(rel_path: str, name: str):
    """用 ast 从服务端源码里取一个**字面量**赋值（不 import，避免拉入 app.* 依赖）。

    ⚠️ 必须同时认 ``ast.Assign`` 与 ``ast.AnnAssign`` —— 服务端不少常量是
    ``_UNITS: Tuple[str, ...] = (...)`` 这种**带类型注解**的写法，
    只认 Assign 会让这些对照用例**静默 skip**（看着全绿，其实没对照）。
    另外 ``_COUNT_UNITS: frozenset = frozenset({...})`` 的值是 Call，要取实参。
    """
    path = os.path.join(_SERVER, rel_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if not (isinstance(target, ast.Name) and target.id == name):
                continue
            value = node.value
            # frozenset({...}) / set({...}) / tuple([...]) 这类包装 → 取实参
            if isinstance(value, ast.Call) and value.args:
                value = value.args[0]
            try:
                return ast.literal_eval(value)
            except ValueError:
                return None
    return None


# ============================================================================
# 1. 输入充分度
# ============================================================================

class TestSufficiency(unittest.TestCase):
    def _req(self, **kw):
        base = {
            "experiences": [], "education": [], "skills": [],
            "certifications": [], "phone": "", "email": "",
            "jd_text": "", "summary": "",
        }
        base.update(kw)
        return base

    def test_empty_input_is_insufficient_with_lowest_target(self):
        r = SUF.assess_input_sufficiency(self._req())
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["level"], "insufficient")
        # 断言**对外契约值**（前端按它显示「目标字数」），不引用被判定的常量自身 ——
        # 否则「把 500 改成 600」这种变异会骗过用例（反向对照 M1 抓到过）。
        self.assertEqual(r["target_length"], 500)
        self.assertEqual(r["min_length"], 400)

    def test_full_input_is_sufficient(self):
        bullets = ["主导 X 系统后端开发并优化性能，接口响应从 800ms 降至 100ms"]
        r = SUF.assess_input_sufficiency(self._req(
            experiences=[{"name": "A 公司", "bullets": bullets * 3}],
            education=[{"school": "某大学", "major": "计算机"}],
            skills=["Python", "Go"],
            certifications=["CET-6"],
            phone="13800000000",
            jd_text="招聘后端工程师",
            summary="三年后端经验",
        ))
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["level"], "sufficient")
        self.assertEqual(r["target_length"], 800)   # 对外契约值，不引用常量自身
        self.assertEqual(r["min_length"], 700)
        self.assertEqual(r["gaps"], [])

    def test_missing_skill_is_named_in_gaps(self):
        r = SUF.assess_input_sufficiency(self._req())
        self.assertIn("缺少技能", r["gaps"])

    def test_few_bullets_reported(self):
        r = SUF.assess_input_sufficiency(self._req(
            experiences=[{"name": "A", "bullets": ["做了一个系统"]}],
        ))
        self.assertTrue(any("要点偏少" in g for g in r["gaps"]))

    def test_short_bullets_reported(self):
        r = SUF.assess_input_sufficiency(self._req(
            experiences=[{"name": "A", "bullets": ["做系统", "写代码", "改bug"]}],
        ))
        self.assertTrue(any("偏简略" in g for g in r["gaps"]))

    def test_weights_match_server(self):
        server = _server_assign("modules/resume_generation/sufficiency.py", "_LEVEL_CONFIG")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(SUF._LEVEL_CONFIG, server)

    def test_accepts_object_like_req(self):
        """兼容对象形状（服务端传 pydantic，桌面传 dict）—— 两条路都要走得通。"""
        class Exp:
            def __init__(self, name, bullets):
                self.name = name
                self.bullets = bullets

        class Req:
            experiences = [Exp("A", ["一", "二", "三"])]
            education = []
            skills = ["Python"]
            certifications = []
            phone = ""
            email = ""
            jd_text = ""
            summary = ""

        r = SUF.assess_input_sufficiency(Req())
        # 经历 15 + 要点≥3条 10 + 技能 15 = 40（要点均长 1 字 < 15，不加分）
        self.assertEqual(
            r["score"], SUF.W_EXPERIENCE + SUF.W_BULLETS + SUF.W_SKILLS)
        self.assertEqual(r["level"], "insufficient")


# ============================================================================
# 2. 方向高频技能词表
# ============================================================================

class TestSkillKeywords(unittest.TestCase):
    def test_direction_skills_equal_job_templates(self):
        """⚠️ 单份数据两处存放：必须逐字相等（漂移即变红）。"""
        for tpl in RG.JOB_TEMPLATES:
            self.assertEqual(
                SK.DIRECTION_SKILLS.get(tpl["key"]),
                tpl["recommendedSkills"],
                "方向 %s 的技能表与 JOB_TEMPLATES 不一致" % tpl["key"],
            )
        self.assertEqual(len(SK.DIRECTION_SKILLS), len(RG.JOB_TEMPLATES))

    def test_top_keywords_by_key(self):
        self.assertEqual(SK.top_keywords("tech", 3), ["Python", "Java", "Go"])

    def test_top_keywords_by_chinese_label(self):
        """服务端用中文标签，桌面用英文 key —— 两种写法都要认。"""
        self.assertEqual(SK.top_keywords("技术研发", 3), SK.top_keywords("tech", 3))

    def test_unknown_direction_returns_empty(self):
        self.assertEqual(SK.top_keywords("不存在的方向"), [])

    def test_directions_lists_all_keys(self):
        self.assertEqual(set(SK.directions()), {t["key"] for t in RG.JOB_TEMPLATES})

    def test_direction_keyword_coverage_is_case_insensitive_and_alias_aware(self):
        """方向高频词覆盖：必须大小写不敏感 + 走 SKILL_ALIASES 别名表。

        ⚠️ 2026-10-11 修的真实缺陷：``quality._score_ats`` 把**展示名**
        （``Python`` / ``PyTorch`` / ``RAG`` …）直接丢给 ``alias_in_text``，
        而 ``text_norm`` 已小写、别名没小写 → **恒判缺失**；含连字符的
        （``scikit-learn``）还会被 ``normalize_text`` 把 ``-`` 换成空格 → 也永远对不上。
        表现是「技能明明写在简历里，却报『方向高频关键词覆盖不足』」。

        **反向对照**：技能缺失的简历必须报缺失 —— 否则本用例成了恒 PASS 的空靶。
        """
        from offerflow_desktop.core.text_utils import alias_in_text, normalize_text

        # 单元级：ASCII 展示名大小写不敏感，且不误伤不存在的能力
        self.assertTrue(alias_in_text("Python", normalize_text("熟悉 python 开发")))
        self.assertTrue(alias_in_text("RAG", normalize_text("做过 rag 检索增强")))
        self.assertFalse(alias_in_text("Rust", normalize_text("熟悉 python 与 java")))

        full = (
            "求职意向 AI 算法工程师 技能 Python PyTorch TensorFlow scikit-learn "
            "transformers langchain rag 大模型 prompt 提示词 微调 lora 向量数据库 "
            "pgvector 自然语言处理 nlp 计算机视觉 机器学习\n"
        ) * 4
        empty = "求职意向 销售 技能 沟通 谈判 客户关系\n" * 6
        st = {"summary": "AI 算法工程师", "skills": [], "experiences": []}

        def ats_coverage_issues(text):
            rep = Q.score_resume_quality(st, text, jd_text="招聘 AI 算法工程师",
                                         direction="ai")
            return [v["issue"] for v in rep.violations
                    if v["dimension"] == "ats" and "高频" in v["issue"]]

        self.assertEqual(ats_coverage_issues(full), [],
                         "技能齐全却仍报「方向高频词覆盖不足」——大小写/别名匹配又坏了")
        self.assertTrue(ats_coverage_issues(empty),
                        "技能缺失却没报缺失——判据成了恒 PASS 的空靶")


# ============================================================================
# 3. 质量评分
# ============================================================================

def _good_structured():
    bullet = "主导订单系统重构，引入 Redis 缓存，接口响应从 800ms 降至 100ms"
    return {
        "summary": "三年后端开发，主导过两个高并发系统的性能优化",
        "skills": ["Python", "Redis", "MySQL"],
        "experiences": [{"name": "A", "role": "后端", "bullets": [bullet, bullet, bullet]}],
    }


def _resume_text(bullet: str, n_bullets: int) -> str:
    """拼一份「结构完整」的简历文本 —— 让 ATS/分节判据拿到高分，
    这样 `passed` 就只取决于被测的那条硬违规（而不是总分不够）。"""
    return (
        "张三\n13800000000 | zhang@example.com\n求职意向\n后端开发\n"
        "个人总结\n三年后端开发经验，主导过两个高并发系统的性能优化与架构升级\n"
        "教育经历\n某大学 计算机科学 本科 2018.09 – 2022.06\n"
        "项目经历\nA（后端）｜2022.07 – 2025.06\n"
        + "\n".join("- " + bullet for _ in range(n_bullets))
        + "\n技能\nPython、Redis、MySQL、FastAPI、Docker\n"
    )


class TestQuality(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(
            Q.W_VERB + Q.W_QUANT + Q.W_RESULT + Q.W_FILLER
            + Q.W_ATS + Q.W_LENGTH + Q.W_ONE_PAGE, 100)

    def test_weak_verb_fails_hard(self):
        """⚠️ 构造必须让**总分 >= 75**：否则 passed=False 可能只是分不够，
        砍掉「弱动词硬违规」后仍然 False，用例就成了空靶（反向对照 M6 抓到过）。"""
        bullet = ("负责订单系统重构，引入 Redis 缓存与异步化，"
                  "接口响应时间从 800ms 降至 100ms，支撑日均 100000 次调用")
        st = {
            "summary": "三年后端开发经验，主导过两个高并发系统的性能优化与架构升级",
            "skills": ["Python", "Redis", "MySQL", "FastAPI", "Docker"],
            "experiences": [{"name": "A", "role": "后端", "bullets": [bullet] * 5}],
        }
        text = (
            "张三\n13800000000 | zhang@example.com\n求职意向\n后端开发\n"
            "个人总结\n三年后端开发经验，主导过两个高并发系统的性能优化与架构升级\n"
            "教育经历\n某大学 计算机科学 本科 2018.09 – 2022.06\n"
            "项目经历\nA（后端）｜2022.07 – 2025.06\n"
            + "\n".join("- " + bullet for _ in range(9))
            + "\n技能\nPython、Redis、MySQL、FastAPI、Docker\n"
        )
        jd = "招聘后端开发工程师，要求熟悉 Python、Redis、MySQL、FastAPI、Docker 与高并发"
        rep = Q.score_resume_quality(st, text, jd_text=jd)
        self.assertGreaterEqual(rep.total_score, Q.PASS_THRESHOLD)  # 分数是够的
        self.assertFalse(rep.passed)                                # 但被硬违规拦下
        self.assertTrue(any("弱动词" in v["issue"] for v in rep.violations))

    def test_short_text_fails_hard(self):
        """同样要「总分够 75」，否则 passed=False 只是因为分低（空靶）。"""
        bullet = ("主导订单系统重构，引入 Redis 缓存与异步化，"
                  "接口响应时间从 800ms 降至 100ms，支撑日均 100000 次调用")
        st = {
            "summary": "三年后端开发经验，主导过两个高并发系统的性能优化与架构升级",
            "skills": ["Python", "Redis", "MySQL", "FastAPI", "Docker"],
            "experiences": [{"name": "A", "role": "后端", "bullets": [bullet] * 5}],
        }
        text = _resume_text(bullet, 7)          # 644 字 < 700
        self.assertLess(len(text.strip()), Q.LENGTH_MIN)
        rep = Q.score_resume_quality(
            st, text, jd_text="招聘后端开发工程师，要求熟悉 Python、Redis、MySQL 与高并发")
        self.assertGreaterEqual(rep.total_score, Q.PASS_THRESHOLD)
        self.assertFalse(rep.passed)
        self.assertTrue(any(v["dimension"] == "length" for v in rep.violations))

    def test_over_one_page_fails_hard(self):
        rep = Q.score_resume_quality(_good_structured(), "x" * 800, is_too_long=True)
        self.assertFalse(rep.passed)
        self.assertTrue(any(v["dimension"] == "one_page" for v in rep.violations))

    def test_clean_resume_passes(self):
        bullet = ("主导订单系统重构，引入 Redis 缓存与异步化，"
                  "接口响应时间从 800ms 降至 100ms，支撑日均 100000 次调用")
        st = {
            "summary": "三年后端开发经验，主导过两个高并发系统的性能优化与架构升级",
            "skills": ["Python", "Redis", "MySQL"],
            "experiences": [{"name": "A", "role": "后端", "bullets": [bullet] * 5}],
        }
        rep = Q.score_resume_quality(
            st, bullet * 14,
            jd_text="招聘后端开发工程师，要求熟悉 Python、Redis、MySQL 与高并发")
        self.assertTrue(rep.passed, "violations=%s" % rep.violations)
        self.assertGreaterEqual(rep.total_score, Q.PASS_THRESHOLD)

    def test_feedback_hint_carries_fact_redline(self):
        st = {"summary": "", "skills": [], "experiences": [
            {"name": "A", "bullets": ["负责后端开发工作"]}]}
        rep = Q.score_resume_quality(st, "x" * 800)
        self.assertIn("事实红线", rep.feedback_hint)
        self.assertIn("绝不允许编造", rep.feedback_hint)

    def test_report_to_dict_shape(self):
        rep = Q.score_resume_quality(_good_structured(), "x" * 800)
        d = Q.report_to_dict(rep)
        self.assertEqual(
            set(d), {"total_score", "passed", "dimensions", "violations", "suggestions"})
        self.assertEqual(
            set(d["dimensions"]),
            {"verb", "quantification", "result", "filler", "ats", "length", "one_page"})

    def test_pass_threshold_matches_server(self):
        src = os.path.join(_SERVER, "modules/resume_generation/quality.py")
        if not os.path.exists(src):
            self.skipTest("服务端源码不可达")
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(">= 75", text)
        self.assertEqual(Q.PASS_THRESHOLD, 75)


# ============================================================================
# 4. 一页约束
# ============================================================================

class TestPageFit(unittest.TestCase):
    def test_header_matches_render_exactly(self):
        """⚠️ 核心对齐判据：与 render 不一致会让块匹配静默退化成全池匹配。"""
        exp = {"name": "某某", "role": "后端开发", "start": "2025.03",
               "end": "2026.06", "type": "project", "bullets": ["主导 X"]}
        rendered = [ln for ln in RG._experience_lines([exp])
                    if not ln.startswith("- ") and ln != "项目经历"]
        self.assertEqual(PF.header_text(exp), rendered[0])

    def test_header_without_period(self):
        self.assertEqual(PF.header_text({"name": "某某", "role": "后端"}),
                         "某某（后端）")

    def test_header_without_name(self):
        self.assertEqual(PF.header_text({"role": "后端", "start": "2025.03"}),
                         "（后端）｜2025.03 – 至今")

    def test_facts_survive_condensation(self):
        """精简后事实层（姓名/角色/起止/公司名）必须原样保留。"""
        base = {
            "name": "张三", "position": "后端", "summary": "旧总结",
            "skills": ["Python", "Go", "K8s"],
            "certifications": ["CET-6"],
            "experiences": [{
                "name": "A 公司", "role": "后端开发", "start": "2024.01", "end": "2025.06",
                "bullets": ["主导订单系统重构，引入 Redis 缓存提升性能",
                            "设计分库分表方案，支撑业务增长需求"],
            }],
        }
        condensed = (
            "张三\n"
            "求职意向\n后端\n"
            "个人总结\n三年后端开发经验\n"
            "项目经历\n"
            "A 公司（后端开发）｜2024.01 – 2025.06\n"
            "- 主导订单系统重构，引入 Redis 缓存提升性能\n"
            "技能\nPython、Go\n"
        )
        new = PF.restructure_from_text(base, condensed)
        exp = new["experiences"][0]
        self.assertEqual(exp["name"], "A 公司")
        self.assertEqual(exp["role"], "后端开发")
        self.assertEqual(exp["start"], "2024.01")
        self.assertEqual(exp["end"], "2025.06")
        self.assertEqual(len(exp["bullets"]), 1)          # 第二条被精简掉了
        self.assertEqual(new["summary"], "三年后端开发经验")
        self.assertEqual(new["skills"], ["Python", "Go"])  # K8s 不在精简稿里 → 删
        self.assertEqual(new["certifications"], [])        # 精简稿里没有 → 删

    def test_degenerate_condensation_falls_back_to_base(self):
        """要点一条都没对上（精简输出不可用）→ 整体回退，绝不产出残缺简历。"""
        base = {"name": "张三", "experiences": [
            {"name": "A 公司", "bullets": ["主导订单系统重构并优化性能"]}]}
        new = PF.restructure_from_text(base, "完全不相关的一段文字")
        self.assertIs(new, base)

    def test_skills_only_shrink(self):
        # base 里带上一个「精简稿里没有」的技能，否则删/不删结果一样（无区分度）；
        # 精简稿必须留一条能与 base 对上的要点，否则会触发**退化守卫**提前返回 base。
        base = {"name": "张三", "skills": ["Python", "K8s"], "certifications": [],
                "experiences": [{"name": "A 公司", "role": "后端",
                                 "bullets": ["主导订单系统重构优化"]}]}
        condensed = ("张三\n项目经历\nA 公司（后端）\n- 主导订单系统重构优化\n"
                     "技能\nPython、Go、Rust\n")
        new = PF.restructure_from_text(base, condensed)
        self.assertEqual(new["skills"], ["Python"])   # K8s 不在精简稿里、Go/Rust 是新增 → 只减不增

    def test_is_too_long_threshold(self):
        self.assertTrue(PF.is_too_long("x" * (PF.ONE_PAGE_MAX_CHARS + 1)))
        self.assertFalse(PF.is_too_long("x" * (PF.ONE_PAGE_MAX_CHARS - 1)))

    def test_optimizer_uses_page_fit_as_single_source(self):
        """优化链路必须**直接接线**到 `core.page_fit`，不许再留第二份一页判据。

        服务端 `optimizer/service.py` 的 `_estimate_too_long` 是这份判据的原始出处；
        桌面侧阶段 1 先把它内联进 `page_fit`，同时在 `services/resume_optimizer`
        保留了一份同名拷贝，两边「必须同结论」靠用例盯着。阶段 2 优化链路复活后
        **按要求让 services 反过来用 core 那一份**（`page_fit` 的 docstring 早就写了），
        所以这里断言的是**单一事实源 + 接线**，而不是「两份拷贝碰巧同值」——
        拷贝一旦分叉，表现是「估出来一页、印出来两页」。

        （旧用例 `test_estimate_too_long_matches_optimizer` 断言的正是那份已删掉的
        拷贝，随重构失效；本用例是它的替代，判据更强。）
        """
        from offerflow_desktop.services import resume_optimizer as OPT
        self.assertFalse(
            hasattr(OPT, "_estimate_too_long"),
            "services 里又出现第二份一页判据，两处一旦分叉就会「估出来一页、印出来两页」",
        )
        self.assertIs(OPT.page_fit, PF, "优化链路没有接到 core 的一页判据上（接线断了）")


# ============================================================================
# 5. bullet 级事实保真
# ============================================================================

class TestFidelity(unittest.TestCase):
    def test_canonical_numbers_normalizes_variants(self):
        got = F.canonical_numbers("十万用户，100000 个用户，三个模块，3 位成员")
        self.assertIn("100000", got)
        self.assertIn("3", got)

    def test_cn_percent_normalized(self):
        self.assertIn("40%", F.canonical_numbers("提升了百分之四十"))

    def test_fabricated_number_is_hard(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["主导系统重构，支撑日均 10 万次调用"]}]}
        rep = F.check_bullet_fidelity(st, source_text="我做过一个系统重构")
        self.assertFalse(rep.passed)
        self.assertEqual(rep.hard_count, 1)

    def test_sourced_number_passes(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["服务 50 家客户"]}]}
        rep = F.check_bullet_fidelity(st, source_text="我服务过 50 家客户")
        self.assertTrue(rep.passed)

    def test_fabricated_year_is_hard(self):
        # 年份**不带「年」字**时才走 year 分支：带「年」的会被「数字+单位」先捕获、
        # 按 number 报（实现刻意不重复报，见 fidelity._YEAR_RE 前的 reported_numbers 判断）
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["主导某系统上线，2021 完成交付"]}]}
        rep = F.check_bullet_fidelity(st, source_text="我在 2024 年做项目")
        self.assertFalse(rep.passed)
        self.assertTrue(any(i["kind"] == "year" for i in rep.issues),
                        "issues=%s" % rep.issues)

    def test_year_with_unit_is_reported_as_number(self):
        """带「年」的年份走 number 分支 —— 两条分支都要覆盖，且**不重复报**。"""
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["2021 年主导某项目上线"]}]}
        rep = F.check_bullet_fidelity(st, source_text="我在 2024 年做项目")
        kinds = [i["kind"] for i in rep.issues]
        self.assertIn("number", kinds)
        self.assertNotIn("year", kinds)   # 已被 number 覆盖，不重复

    def test_role_claim_is_warn_not_hard(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["作为技术负责人主导重构"]}]}
        rep = F.check_bullet_fidelity(st, source_text="参与开发", source_roles=["开发"])
        self.assertTrue(rep.passed)  # warn 不算 hard
        self.assertTrue(any(i["kind"] == "role" for i in rep.issues))

    def test_role_claim_allowed_when_source_says_so(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["作为技术负责人主导重构"]}]}
        rep = F.check_bullet_fidelity(
            st, source_text="技术负责人", source_roles=["技术负责人"])
        self.assertFalse(any(i["kind"] == "role" for i in rep.issues))

    def test_sanitize_removes_only_fabricated(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["服务 50 家客户，提升 40%"]}]}
        _, removed = F.sanitize_structured(st, source_text="我服务过 50 家客户")
        self.assertIn("40%", removed)
        self.assertNotIn("50", removed)
        self.assertIn("50", st["experiences"][0]["bullets"][0])

    def test_sanitize_is_idempotent(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["服务 50 家客户，提升 40%"]}]}
        F.sanitize_structured(st, source_text="我服务过 50 家客户")
        first = st["experiences"][0]["bullets"][0]
        _, removed2 = F.sanitize_structured(st, source_text="我服务过 50 家客户")
        self.assertEqual(st["experiences"][0]["bullets"][0], first)
        self.assertEqual(removed2, [])

    def test_feedback_hint_lists_redline(self):
        st = {"summary": "", "experiences": [
            {"name": "A", "bullets": ["支撑日均 10 万次调用"]}]}
        rep = F.check_bullet_fidelity(st, source_text="做过系统")
        self.assertIn("编造", rep.feedback_hint)
        self.assertIn("绝不允许编造", rep.feedback_hint)


# ============================================================================
# 6. 与服务端同源（源码不可达时 skip）
# ============================================================================

class TestServerParity(unittest.TestCase):
    def test_sufficiency_config_identical(self):
        server = _server_assign("modules/resume_generation/sufficiency.py", "_LEVEL_CONFIG")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(SUF._LEVEL_CONFIG, server)

    def test_quality_weak_verbs_identical(self):
        server = _server_assign("modules/resume_generation/quality.py", "WEAK_VERB_STARTS")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(Q.WEAK_VERB_STARTS, set(server))

    def test_quality_strong_verbs_identical(self):
        server = _server_assign("modules/resume_generation/quality.py", "STRONG_VERB_STARTS")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(Q.STRONG_VERB_STARTS, set(server))

    def test_quality_filler_identical(self):
        server = _server_assign("modules/resume_generation/quality.py", "FILLER_PHRASES")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(Q.FILLER_PHRASES, set(server))

    def test_fidelity_units_identical(self):
        server = _server_assign("modules/resume_generation/fidelity.py", "_UNITS")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(F._UNITS, tuple(server))

    def test_fidelity_count_units_identical(self):
        server = _server_assign("modules/resume_generation/fidelity.py", "_COUNT_UNITS")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(F._COUNT_UNITS, frozenset(server))

    def test_page_fit_match_threshold_identical(self):
        server = _server_assign("modules/resume_generation/page_fit.py", "_MATCH_THRESHOLD")
        if server is None:
            self.skipTest("服务端源码不可达")
        self.assertEqual(PF._MATCH_THRESHOLD, server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
