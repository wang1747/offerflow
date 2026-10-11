"""AI 语义评分：对正则判不了的语义质量维度，用 LLM 打分。

与 ``core/quality.py``（正则硬指标）互补，构成「代码规则 + AI 语义」两层评分：
- ``core/quality.py`` 判「字数/量化数字/动词开头/空话/事实保真」——正则 100% 准，AI 反而数不清。
- 本模块判「结果可信度/表达质量/岗位契合度」——需要理解语义，正则做不到。

关键：AI 打分本身也被代码约束——
1. 强制结构化 JSON schema（不允许自由发挥）；
2. 代码解析 + 逐字段校验 + 分数 clamp 到 0~100；
3. 失败静默降级（返回 {}），绝不阻断主流程；
4. 结果可回归、可对比（每次都是同构的 dict）。

分层原则（来自「精确率 vs 召回率」）：
正则 = 高精确率（判对的必对）但低召回率（漏判多）；
AI   = 高召回率（覆盖全）但精确率不稳定（概率性）；
两者分开打分、分开展示，谁都不含糊。

=====================================================================
与 Web 版 ``backend/app/modules/resume_generation/semantic.py`` 同源。
搬运方式：服务端用 langchain ``llm.invoke([SystemMessage, HumanMessage])``，
桌面用 ``llm.chat(system, user)`` 直连 OpenAI 兼容接口（BYOK），已改写为同步。
提示词文本逐字一致；条款挂载口径也一致（**只挂防注入**）。
=====================================================================
"""

import json
from typing import Any, Callable, Dict, Optional

from ..llm import chat as _default_chat
from ..llm import parse_json
from .prompts import _INJECTION_GUARD, _attach

SEMANTIC_SYSTEM_PROMPT = """你是资深招聘官与简历评审专家。请对一份中文简历做「语义质量」评分——这些是规则无法判断、必须理解语义才能判断的维度。

只输出一个 JSON 对象，不要输出任何其他内容，结构如下：
{
  "result_credibility": {"score": 整数0-100, "reason": "一句话理由"},
  "expression_quality": {"score": 整数0-100, "reason": "一句话理由"},
  "jd_fit": {"score": 整数0-100, "reason": "一句话理由"},
  "overall": "一句话总评"
}

维度说明：
1. result_credibility（结果可信度）：各条经历的成果是否真实可信、是否量化、是否与「做了什么」强相关（而非空泛的「提升了效率」）。满分 = 每条都有可信、相关、可量化的结果。
2. expression_quality（表达质量）：表达是否自然、专业、有信息量；是否避免了「负责/参与/协助」等弱表达与「认真负责/吃苦耐劳」等空话套话；是否有生硬套模板的痕迹。
3. jd_fit（岗位契合度）：整份简历是否真正匹配目标岗位的要求——不是关键词是否出现，而是经历是否体现了 JD 要求的能力与经验；无 JD 时按「通用岗位」评估。

评分规则：
- 只基于「用户真实填写的信息」评分，信息少时不要臆想信息多，但可以在 reason 里指出「信息不足」。
- 分数要拉开差距：确实优秀给 85+，明显薄弱给 60 以下，不要都打 80。
- reason 一句话说清「这个分的原因 + 主要短板」。
"""


def build_semantic_user_prompt(
    structured: Dict[str, Any], resume_text: str, jd_text: str
) -> str:
    return json.dumps(
        {
            "position": structured.get("position", ""),
            "skills": structured.get("skills", []),
            "resume_text": resume_text,
            "jd_text": jd_text or "",
        },
        ensure_ascii=False,
    )


def _clamp_score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def score_semantic_quality(
    structured: Dict[str, Any],
    resume_text: str,
    jd_text: str = "",
    chat_fn: Optional[Callable[[str, str], str]] = None,
) -> Dict[str, Any]:
    """调用 LLM 做语义评分，返回结构化 dict；任何异常都返回空 dict（静默降级）。

    三个维度：result_credibility / expression_quality / jd_fit，各含 score(0-100) + reason。
    额外给出 overall 一句话总评 与 total_score（三维度平均）。

    ``chat_fn`` 是给测试注入假模型用的（默认走 ``llm.chat``）。
    """
    try:
        fn = chat_fn or _default_chat
        raw = fn(
            SEMANTIC_SYSTEM_PROMPT,
            build_semantic_user_prompt(structured, resume_text, jd_text),
        )
        data = parse_json(raw)
    except Exception:  # noqa: BLE001
        return {}

    if not isinstance(data, dict):
        return {}

    out: Dict[str, Any] = {}
    for key in ("result_credibility", "expression_quality", "jd_fit"):
        item = data.get(key)
        if isinstance(item, dict):
            out[key] = {
                "score": _clamp_score(item.get("score")),
                "reason": str(item.get("reason", ""))[:200],
            }

    if isinstance(data.get("overall"), str):
        out["overall"] = data["overall"][:300]

    if out:
        scores = [
            out[k]["score"]
            for k in ("result_credibility", "expression_quality", "jd_fit")
            if k in out
        ]
        if scores:
            out["total_score"] = round(sum(scores) / len(scores))

    return out


# ============ 共用条款挂载 ============
#
# ⚠️ 这里**只挂防注入**，绝不能挂 _NO_SCORE_RULE：本模块的设计目标就是让 LLM 打
# 三个维度的 0~100 分，挂上禁给分它会拒绝输出 score，整个语义评分直接失效
# （与桌面版 MATCH_SEMANTIC_SYSTEM 同一个道理）。
# 用户消息里会拼进简历正文与 JD 原文，所以防注入是必须的。

SEMANTIC_SYSTEM_PROMPT = _attach(SEMANTIC_SYSTEM_PROMPT, _INJECTION_GUARD)
