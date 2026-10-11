"""薪资参考 LLM 调用：从简历提取画像 + 生成解读文案。

职责边界（关键）：
- LLM 只负责「读简历」：提取学历 / 学校层次 / 求职方向 / 亮点，以及生成解读文案。
- **薪资数字不由 LLM 算**，而是由 `app.core.salary_benchmark` 根据提取结果查表计算，
  本模块的解读函数只接收后端算好的区间，负责「解释为什么是这个价」。
"""

import json
import logging
import re
from typing import Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import get_user_llm_or_raise
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.models.user import User
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)


EXTRACT_PROFILE_SYSTEM_PROMPT = """你是资深 HR 薪酬分析师。请阅读候选人的简历，提取用于「定薪」的结构化信息。

只提取事实，不要评价，不要给出薪资数字。

请从下面这些选项里选，不要自己编造选项：
- degree（学历）：大专 / 本科 / 硕士 / 博士
- school_tier（学校层次）：985/211 / 普通本科 / 专科
- direction（求职方向，从下面选一个最贴切的）：
  AI/算法、前端、技术研发、半导体/芯片、产品经理、设计、运营/新媒体、市场/销售、
  金融/财会、法律、行政/人事、教育/教师、医疗/护理、机械/制造、电气/自动化、
  土木/建筑、传媒/新闻、环境/环保、物流/采购、通用
- position_raw（目标岗位名，如「后端开发工程师」）
- target_city_hint（简历中体现的城市意向，如「北京」，没有就留空字符串）
- work_years（工作年限，数字，如应届/实习为 0，全职工作 2 年则为 2，无法判断填 0）
- highlights（3~5 条加分/减分点，用短语，如「2 段实习经历」「大模型相关项目」）

只输出 JSON，不要其他任何文字，格式如下：
{"degree":"本科","school_tier":"普通本科","direction":"技术研发","position_raw":"后端开发工程师","target_city_hint":"北京","work_years":2,"highlights":["...","..."]}"""


ANALYSIS_SYSTEM_PROMPT = """你是资深薪酬顾问。下面是根据候选人简历、按市场行情算出的薪资参考，请你把它讲给一位「不知道自己该怎么要价」的求职者听。

候选人画像：
学历：{degree_label}；学校：{school_label}；经验：{experience_label}；求职方向：{direction_label}；目标岗位：{position}
城市：{city_label}；简历亮点：{highlights}

市场参考（税前月薪，单位元）：
合理区间：{low} ~ {high} 元；建议开口价：{ask} 元；目标价（期望落地）：{target} 元；底线：{bottom} 元
定薪依据：{basis}

请用通俗、鼓励的口吻输出，分四个小段，每段加一个小标题：
1. 【你的市场价】一句话告诉 TA 这个价位是什么水平（普通/不错/有竞争力），并结合学历+经验+方向+城市解释为什么
2. 【你的筹码与短板】结合简历亮点，说清哪些能帮 TA 多要一点、哪些会拖后腿
3. 【怎么开口】告诉 TA：第一次开口要报「开口价 {ask} 元」而不是「目标价 {target} 元」——因为 HR 一定会往下压，报高一点（开口价）才留出被压价的空间，最后才可能落在目标价附近。给出具体报价话术（用「价值」而非「我需要钱」），和第一次被压价时的应对
4. 【底线提醒】解释为什么底线不要轻易破，以及这些数字是税前、不含年终奖和补贴

要求：
- 数字只引用上面给出的数字，不要自己编新的薪资数字
- 口语化、接地气，别堆砌术语，控制在 300 字以内"""

# 两条都吃简历文本（EXTRACT 直读简历原文，ANALYSIS 拿简历亮点）⇒ 都挂防注入。
# ⚠️ ANALYSIS 之后会走 str.format(...)，条款文本必须零花括号。
# 两者都不产出简历正文；EXTRACT 本身已写明"只提取事实、不评价、不给薪资数字"，
# 无需再挂 NO_SCORE。数字由 app.core.salary_benchmark 查表算，本来就不经 LLM。
EXTRACT_PROFILE_SYSTEM_PROMPT = attach(EXTRACT_PROFILE_SYSTEM_PROMPT, INJECTION_GUARD)
ANALYSIS_SYSTEM_PROMPT = attach(ANALYSIS_SYSTEM_PROMPT, INJECTION_GUARD)


def _clean_json(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def _parse_profile(raw: str) -> Dict:
    text = _clean_json(raw)
    # 截取首个 { 到最后一个 }，容错 LLM 前后夹带文字
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def extract_resume_profile(
    resume_text: str,
    position_hint: str,
    user: User,
) -> Dict:
    """从简历文本提取定薪画像（学历/学校/方向/亮点），不涉及薪资数字。"""
    human_parts = [f"简历内容：\n{resume_text}"]
    if position_hint:
        human_parts.append(f"\n候选人心仪岗位：{position_hint}")
    human = "\n".join(human_parts)

    try:
        llm = get_user_llm_or_raise(user)
        with trace_operation("salary_reference_extract", getattr(user, "id", None)):
            response = llm.invoke(
                [SystemMessage(content=EXTRACT_PROFILE_SYSTEM_PROMPT), HumanMessage(content=human)]
            )
        profile = _parse_profile(response.content)
        if not profile:
            logger.warning("简历画像提取失败，返回空 profile: %s", response.content[:200])
        return profile
    except ValueError as e:
        logger.warning("提取简历画像失败（未配置模型）: %s", e)
        return {}
    except Exception as e:
        logger.error("提取简历画像异常: %s", e)
        return {}


def generate_salary_analysis(
    degree_label: str,
    school_label: str,
    direction_label: str,
    position: str,
    city_label: str,
    experience_label: str,
    highlights: List[str],
    low: int,
    high: int,
    ask: int,
    target: int,
    bottom: int,
    basis: str,
    user: User,
) -> str:
    """生成通俗解读文案（数字由后端传入，LLM 只解释）。"""
    highlights_text = "；".join(highlights) if highlights else "未识别到明显亮点"
    system = ANALYSIS_SYSTEM_PROMPT.format(
        degree_label=degree_label,
        school_label=school_label,
        direction_label=direction_label,
        position=position or "未指定岗位",
        city_label=city_label,
        experience_label=experience_label,
        highlights=highlights_text,
        low=low,
        high=high,
        ask=ask,
        target=target,
        bottom=bottom,
        basis=basis,
    )
    try:
        llm = get_user_llm_or_raise(user)
        with trace_operation("salary_reference_analysis", getattr(user, "id", None)):
            response = llm.invoke([SystemMessage(content=system), HumanMessage(content="请给出薪资参考解读。")])
        return (response.content or "").strip()
    except ValueError as e:
        logger.warning("生成薪资解读失败（未配置模型）: %s", e)
        return ""
    except Exception as e:
        logger.error("生成薪资解读异常: %s", e)
        return ""
