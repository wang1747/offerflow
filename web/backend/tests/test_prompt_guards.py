"""提示词共用条款（``app/core/prompt_guards.py``）的判据。

防三类真实故障：

1. **漏挂** —— 某条链路的提示词没带上防注入 / 占位符 / 禁给分，线上留缺口。
2. **误挂** —— 给"本身就是给分模块"的 ``semantic.py`` 挂上 NO_SCORE，
   模型会拒绝输出 score，整个语义评分直接失效。
3. **条款含花括号** —— ``salary_negotiation`` / ``salary_reference`` 的提示词
   走 ``str.format()``，条款里一旦出现 ``{`` 会抛 KeyError，整条链路炸掉。
   这个坑的特别之处：**只有真去调用 format 才会暴露**，光看代码看不出来 ——
   所以下面专门有一类是"真的 format 一次"。

另有一条容易被忽略的：``conftest.FakeLLM`` 靠**判别串**路由（如「JD（职位描述）解析器」）。
条款是追加在**末尾**的，理论上不会挤掉判别串，但"理论上"不算证据 ——
``TestFakeLlmMarkersSurvive`` 实测一遍（判别串一旦失配，测试会静默走兜底分支，全绿却什么都没测到）。

每个"该挂"的判据都配了反向对照：把缺陷放回去，判据必须变红。
"""

import importlib.util
import string
from contextlib import contextmanager
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.core import prompt_guards as pg


# ============ 工具 ============


def _format_kwargs(template: str) -> dict:
    """抠出模板里所有 ``{字段名}``，生成一份"随便填"的参数字典。

    用 ``string.Formatter`` 的真实解析结果，而不是手写参数名 —— 手写一定会写错
    或写漏，那时测的是"我记不记得参数名"，不是"模板能不能 format"。
    """
    return {name: "x" for _, name, _, _ in string.Formatter().parse(template) if name}


@contextmanager
def _noop_trace(*args, **kwargs):
    """替掉 trace_operation：它成功路径会落库，离线用例不需要。"""
    yield None


class _CaptureLLM:
    """只记录"真正发给模型的 system 内容"，不做任何解析。

    用途：两处提示词是**函数内的局部变量**（jd_analysis / interview.services），
    没法 import 出来断言。直接抓"实际发出去的那份"比读源码可靠。
    """

    def __init__(self):
        self.systems = []

    def invoke(self, messages):
        for m in messages:
            if type(m).__name__ == "SystemMessage":
                self.systems.append(m.content)
        return AIMessage(content="{}")


ALL_GUARDS = (
    (pg.INJECTION_GUARD, "防注入"),
    (pg.PLACEHOLDER_RULE, "占位符"),
    (pg.NO_SCORE_RULE, "禁给分"),
)


# ============ 一、条款文本自身的性质 ============


class TestGuardTexts:
    def test_three_guards_are_non_empty(self):
        assert pg.INJECTION_GUARD.strip()
        assert pg.PLACEHOLDER_RULE.strip()
        assert pg.NO_SCORE_RULE.strip()

    def test_guards_start_with_blank_line(self):
        """条款必须以空行开头，否则追加后会与提示词正文黏在同一行。"""
        for guard, name in ALL_GUARDS:
            assert guard.startswith("\n\n"), f"{name}条款没有以空行开头"

    def test_guards_have_no_braces(self):
        """★ 零花括号：走 str.format() 的提示词里出现 ``{`` 会直接炸掉链路。"""
        for guard, name in ALL_GUARDS:
            assert "{" not in guard, f"{name}条款含左花括号，会让 format() 抛 KeyError"
            assert "}" not in guard, f"{name}条款含右花括号，会让 format() 抛 KeyError"

    def test_import_time_selfcheck_rejects_braces(self, tmp_path):
        """导入期自检：条款一旦含花括号，**模块导入就失败**。

        为什么要有这条：光靠测试拦，风险窗口是"从改完到有人跑测试"；
        有了自检，窗口缩到"改完到下次启动"，且是 fail fast（部署时立刻暴露），
        不会变成"上线后某个用户用到某条链路才炸"。

        这里造一份**变异副本**来验自检逻辑，不碰真源码。
        """
        src = Path(pg.__file__).read_text(encoding="utf-8")
        mutated = src.replace('INJECTION_GUARD = """', 'INJECTION_GUARD = """{oops}', 1)
        assert mutated != src, "变异锚点没找到（源码里的常量写法变了？）"

        target = tmp_path / "pg_mutated.py"
        target.write_text(mutated, encoding="utf-8")

        spec = importlib.util.spec_from_file_location("pg_mutated", target)
        mod = importlib.util.module_from_spec(spec)
        with pytest.raises(ValueError, match="含花括号"):
            spec.loader.exec_module(mod)

    def test_attach_is_idempotent(self):
        """重复挂载不叠加：模块被重复导入 / 热重载时不能把条款叠成好几份。"""
        once = pg.attach("正文", pg.INJECTION_GUARD)
        twice = pg.attach(once, pg.INJECTION_GUARD)
        assert once == twice
        assert once.count(pg.INJECTION_GUARD) == 1

    def test_attach_keeps_body_intact(self):
        """挂载只在末尾追加，不得动正文一个字。"""
        body = "你是专家。\n\n要求：只输出 JSON。"
        assert pg.attach(body, pg.INJECTION_GUARD).startswith(body)


# ============ 二、该挂的挂了 ============


class TestMountedWhereNeeded:
    """产出简历正文的提示词：三条全挂。"""

    def test_resume_generation_has_all_three(self):
        from app.modules.resume_generation import prompts

        for attr in ("SYSTEM_PROMPT", "REGENERATE_SYSTEM_PROMPT",
                     "REGENERATE_VARIANTS_SYSTEM_PROMPT"):
            text = getattr(prompts, attr)
            for guard, name in ALL_GUARDS:
                assert guard in text, f"resume_generation.{attr} 缺{name}条款"

    def test_optimizer_has_all_three(self):
        from app.modules.resume.optimizer import prompts as opt

        for attr in ("SYSTEM_PROMPT", "CONDENSE_SYSTEM_PROMPT"):
            text = getattr(opt, attr)
            for guard, name in ALL_GUARDS:
                assert guard in text, f"optimizer.{attr} 缺{name}条款"

    def test_semantic_has_injection_only(self):
        from app.modules.resume_generation import semantic

        assert pg.INJECTION_GUARD in semantic.SEMANTIC_SYSTEM_PROMPT

    def test_injection_only_modules(self):
        """吃外部内容、但不产出简历正文 / 不打分的：只挂防注入。"""
        from app.agents.graphs import (
            interview_prep,
            mock_interview,
            salary_negotiation,
            salary_reference,
        )
        from app.modules.jd import vision_service
        from app.modules.resume.optimizer import structure_extract

        targets = [
            ("structure_extract.EXTRACT_SYSTEM_PROMPT", structure_extract.EXTRACT_SYSTEM_PROMPT),
            ("vision_service.VISION_SYSTEM_PROMPT", vision_service.VISION_SYSTEM_PROMPT),
            ("interview_prep.SYSTEM_PROMPT", interview_prep.SYSTEM_PROMPT),
            ("mock_interview.INTERVIEWER_PROMPT", mock_interview.INTERVIEWER_PROMPT),
            ("mock_interview.EVALUATOR_PROMPT", mock_interview.EVALUATOR_PROMPT),
            ("mock_interview.SUMMARIZER_PROMPT", mock_interview.SUMMARIZER_PROMPT),
            ("salary_negotiation.HR_SYSTEM_PROMPT", salary_negotiation.HR_SYSTEM_PROMPT),
            ("salary_negotiation.COACH_SYSTEM_PROMPT", salary_negotiation.COACH_SYSTEM_PROMPT),
            ("salary_negotiation.SUMMARY_SYSTEM_PROMPT", salary_negotiation.SUMMARY_SYSTEM_PROMPT),
            ("salary_reference.EXTRACT_PROFILE_SYSTEM_PROMPT",
             salary_reference.EXTRACT_PROFILE_SYSTEM_PROMPT),
            ("salary_reference.ANALYSIS_SYSTEM_PROMPT",
             salary_reference.ANALYSIS_SYSTEM_PROMPT),
        ]
        for label, text in targets:
            assert pg.INJECTION_GUARD in text, f"{label} 缺防注入条款"

    def test_jd_analysis_inline_prompt_reaches_model_with_guard(self, monkeypatch):
        """jd_analysis 的提示词是函数内局部变量 ⇒ 直接抓发给模型的那份。"""
        import app.agents.graphs.jd_analysis as jd_analysis

        cap = _CaptureLLM()
        monkeypatch.setattr(jd_analysis, "get_user_llm_or_raise", lambda user: cap)

        jd_analysis.parse_jd_node({"raw_text": "某 JD 原文"}, {"configurable": {"user": object()}})

        assert cap.systems, "jd_analysis 根本没发出 system 消息（判据打空靶）"
        assert pg.INJECTION_GUARD in cap.systems[-1], "jd_analysis 的 system 缺防注入条款"

    def test_interview_extract_inline_prompt_reaches_model_with_guard(self, monkeypatch):
        """面经元数据提取的提示词同样是函数内局部变量。"""
        import app.modules.interview.services as interview_service

        cap = _CaptureLLM()
        monkeypatch.setattr(interview_service, "get_user_llm_or_raise", lambda user: cap)
        monkeypatch.setattr(interview_service, "trace_operation", _noop_trace)

        interview_service.extract_article_metadata("某面经原文", object())

        assert cap.systems, "面经提取根本没发出 system 消息（判据打空靶）"
        assert pg.INJECTION_GUARD in cap.systems[-1], "面经提取的 system 缺防注入条款"


# ============ 三、不该挂的确实没挂（设计决策，防后来人"顺手补上"） ============


class TestNotMountedByDesign:
    def test_semantic_must_not_carry_no_score(self):
        """★ semantic 本身就是给分模块（三维度 0~100）。

        挂上禁给分，模型会拒绝输出 score ⇒ 整个语义评分失效。
        与桌面版 MATCH_SEMANTIC_SYSTEM 是同一个坑。
        """
        from app.modules.resume_generation import semantic

        assert pg.NO_SCORE_RULE not in semantic.SEMANTIC_SYSTEM_PROMPT, (
            "semantic.py 被挂上了禁给分条款 —— 它会让模型拒绝输出 score，"
            "整个语义评分直接失效（参看桌面版 MATCH_SEMANTIC_SYSTEM 的处理）"
        )

    def test_benchmark_refresh_has_no_guard(self):
        """输入是我们自己的基准数据（不受用户控制）；且模板花括号密集。"""
        from app.agents.graphs import salary_benchmark_refresh as refresh

        assert pg.INJECTION_GUARD not in refresh.REFRESH_SYSTEM_PROMPT

    def test_career_agent_keeps_its_own_guard(self):
        """career_agent 的 CONTEXT_PROMPT 自带等效条款，不重复挂。"""
        from app.modules.career_agent import graph

        assert pg.INJECTION_GUARD not in graph.CONTEXT_PROMPT, "重复挂载会让提示词冗余"
        assert "不可信数据" in graph.CONTEXT_PROMPT, (
            "career_agent 原有的防注入说明不见了 —— 它是那边的唯一防线"
        )


# ============ 四、format 实调（★ 花括号问题只有这一步能暴露） ============


class TestFormatStillWorks:
    """真正调用一次 ``str.format()``。

    这一类的价值：条款文本是**常量**，即便含花括号，光 import 也不会报错；
    只有实际拼装提示词的那一刻才会 KeyError。所以判据必须落在"真的 format 一次"。
    """

    def _assert_formats(self, template: str, label: str):
        template.format(**_format_kwargs(template))  # 抛异常即判据失败
        assert pg.INJECTION_GUARD in template, f"{label} 缺防注入条款"

    def test_salary_negotiation_hr_formats(self):
        from app.agents.graphs.salary_negotiation import HR_SYSTEM_PROMPT

        self._assert_formats(HR_SYSTEM_PROMPT, "salary_negotiation.HR")

    def test_salary_negotiation_coach_formats(self):
        from app.agents.graphs.salary_negotiation import COACH_SYSTEM_PROMPT

        self._assert_formats(COACH_SYSTEM_PROMPT, "salary_negotiation.COACH")

    def test_salary_reference_analysis_formats(self):
        from app.agents.graphs.salary_reference import ANALYSIS_SYSTEM_PROMPT

        self._assert_formats(ANALYSIS_SYSTEM_PROMPT, "salary_reference.ANALYSIS")

    def test_benchmark_refresh_formats(self):
        from app.agents.graphs.salary_benchmark_refresh import REFRESH_SYSTEM_PROMPT

        REFRESH_SYSTEM_PROMPT.format(**_format_kwargs(REFRESH_SYSTEM_PROMPT))

    def test_reverse_brace_would_break_a_real_call_site(self):
        """反向对照：条款若含花括号，**真实调用点**（只传 scenario）必然 KeyError。

        这条证明上面那几个 format 判据是有区分度的 —— 它们不是在"按构造必然通过"。
        """
        from app.agents.graphs.salary_negotiation import HR_SYSTEM_PROMPT

        broken = HR_SYSTEM_PROMPT + "{oops}"
        with pytest.raises(KeyError):
            broken.format(scenario="初次谈薪")  # 调用点就只传这一个参数


# ============ 五、FakeLLM 判别串不许被挤掉 ============


class TestFakeLlmMarkersSurvive:
    """条款追加在末尾，**不能挤掉** conftest.FakeLLM 赖以路由的判别串。

    判别串一旦失配，会静默走兜底分支 —— 用例照样"全绿"，却什么都没测到
    （conftest 里记着这个教训：曾导致 test_jd_parse_api 报 KeyError: 'company'）。
    """

    MARKERS = [
        ("app.modules.resume_generation.prompts", "SYSTEM_PROMPT", "资深简历撰写专家"),
        ("app.modules.resume_generation.prompts", "REGENERATE_SYSTEM_PROMPT", "请只重新生成"),
        ("app.modules.resume.optimizer.prompts", "SYSTEM_PROMPT", "简历优化专家"),
        ("app.modules.resume.optimizer.prompts", "CONDENSE_SYSTEM_PROMPT", "简历精简专家"),
        ("app.modules.resume.optimizer.structure_extract", "EXTRACT_SYSTEM_PROMPT", "简历解析专家"),
        ("app.agents.graphs.interview_prep", "SYSTEM_PROMPT", "面试题生成专家"),
        ("app.agents.graphs.mock_interview", "INTERVIEWER_PROMPT", "专业的面试官"),
        ("app.agents.graphs.mock_interview", "SUMMARIZER_PROMPT", "资深面试官"),
        ("app.modules.resume_generation.semantic", "SEMANTIC_SYSTEM_PROMPT", "语义质量"),
    ]

    def test_importable_markers_present(self):
        import importlib

        for mod_name, attr, marker in self.MARKERS:
            mod = importlib.import_module(mod_name)
            text = getattr(mod, attr)
            assert marker in text, (
                f"{mod_name}.{attr} 丢了判别串「{marker}」—— "
                "conftest.FakeLLM 会路由失败并静默走兜底分支"
            )

    def test_jd_analysis_marker_present_in_live_prompt(self, monkeypatch):
        """jd_analysis 的判别串在函数内，抓真实发出的那份来验。"""
        import app.agents.graphs.jd_analysis as jd_analysis

        cap = _CaptureLLM()
        monkeypatch.setattr(jd_analysis, "get_user_llm_or_raise", lambda user: cap)
        jd_analysis.parse_jd_node({"raw_text": "某 JD"}, {"configurable": {"user": object()}})

        assert "JD（职位描述）解析器" in cap.systems[-1], (
            "jd_analysis 丢了判别串「JD（职位描述）解析器」"
        )
