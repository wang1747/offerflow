"""alpha 阶段的结构化输出契约与校验。

设计依据：设计文档第 11 节（输出约束）与 20.4 节（alpha 输出格式）。

两条硬要求：
1. **所有 LLM 输出必须通过 Pydantic 校验**；校验失败**最多修复 1 次**，
   仍失败则判 alpha 失败（22.5 节）——不能无限重试。
2. ``decision`` 只允许 5 个枚举值；``evidence_refs`` 必须能在 ToolResult 中找到，
   否则视为**伪造证据**（22.5 节直接判失败）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.modules.career_agent.state import ActionCandidate, Evidence

logger = logging.getLogger(__name__)

#: 设计文档 20.4 节：decision 只允许这 5 个值
DecisionKind = Literal[
    "apply_now",        # 值得投递
    "optimize_first",   # 先优化简历
    "collect_evidence", # 补证据
    "wait",             # 等待
    "skip",             # 跳过
]

DECISIONS = frozenset({
    "apply_now", "optimize_first", "collect_evidence", "wait", "skip",
})


#: 建议行动直接复用 ``state.ActionCandidate``（单一事实源）。
#: ⚠️ 不要在这里另建一个 Action 类——两份同名同义的模型会导致
#:    Pydantic 跨类赋值被拒（Input should be a valid instance of ActionCandidate），
#:    且违反"同一实体只有一个模型"。本项目已实测踩过。
Action = ActionCandidate


class AgentDecision(BaseModel):
    """alpha 阶段唯一对外的结构化输出。"""

    decision: DecisionKind
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    actions: List[Action] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    missing_info: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_decision(self) -> "AgentDecision":
        if self.decision not in DECISIONS:
            raise ValueError(f"非法 decision：{self.decision}")
        return self


class ValidationReport(BaseModel):
    """校验结果——让失败原因可观测，而不是只抛异常。"""

    ok: bool
    errors: List[str] = Field(default_factory=list)
    repaired: bool = False          # 是否经过一次修复
    unrecoverable: bool = False     # 修复后仍失败 → alpha 判失败


# ===========================================================================
# 解析 / 修复 / 校验
# ===========================================================================
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(raw: str) -> str:
    """从模型输出里抠出 JSON。模型常带 ```json 围栏或前后废话。"""
    if not raw:
        return ""
    text = _FENCE.sub("", raw.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start:end + 1]


def parse_decision(raw: str) -> AgentDecision:
    """解析模型输出为 AgentDecision；失败抛 ``ValueError``。"""
    return AgentDecision(**json.loads(_extract_json(raw)))


def validate_evidence(
    decision: AgentDecision,
    allowed_refs: List[str],
) -> ValidationReport:
    """校验所有证据引用是否真实存在。

    设计文档 22.5 节：action 引用**不存在、过期或被伪造**的证据 → alpha 直接失败。
    这里做**双向校验**：decision 的每条 evidence.ref 与每个 action 的
    evidence_refs 都必须落在允许集合里。

    ⚠️ 允许集合为空时**不是放行**：那意味着本轮没有任何工具成功返回，
    因而**不存在任何合法引用**，此时模型给出的任何 ref 都是编造的。
    早期实现写成 ``if allowed and ...``，等于「没有工具结果就随便编」——
    这是真模型验证中实测到的漏洞，已收紧。
    """
    errors: List[str] = []
    allowed = set(allowed_refs or [])

    if not allowed:
        for ev in decision.evidence:
            errors.append(f"本轮没有任何工具结果，但输出引用了证据 {ev.ref}（疑似伪造）")
        for act in decision.actions:
            if act.evidence_refs:
                errors.append(f"行动「{act.title}」在无工具结果时引用了证据（疑似伪造）")
        return ValidationReport(ok=not errors, errors=errors)

    for ev in decision.evidence:
        if ev.ref not in allowed:
            errors.append(f"证据 {ev.ref} 不在工具返回的引用集合中（疑似伪造）")

    for act in decision.actions:
        for ref in act.evidence_refs:
            if ref not in allowed:
                errors.append(f"行动「{act.title}」引用了不存在的证据 {ref}")

    return ValidationReport(ok=not errors, errors=errors)


def validate_output(
    raw: str,
    allowed_refs: Optional[List[str]] = None,
    attempt: int = 0,
) -> tuple[Optional[AgentDecision], ValidationReport]:
    """校验模型输出，**最多修复 1 次**（22.5 节）。

    返回 ``(decision | None, report)``。第一次失败会重试解析（如能修复围栏等
    格式问题）；仍失败则 ``unrecoverable=True``，由调用方判 alpha 失败。
    """
    if attempt > 1:
        return None, ValidationReport(
            ok=False, errors=["超出修复次数上限（最多 1 次）"],
            repaired=True, unrecoverable=True,
        )

    try:
        decision = parse_decision(raw)
    except Exception as e:  # noqa: BLE001
        msg = f"结构化输出解析失败：{type(e).__name__}: {e}"
        return None, ValidationReport(ok=False, errors=[msg], repaired=attempt > 0)

    report = validate_evidence(decision, allowed_refs or [])
    if report.ok:
        return decision, ValidationReport(ok=True, repaired=attempt > 0)

    # 证据校验不过也只给 1 次修复机会
    return None, ValidationReport(
        ok=False, errors=report.errors, repaired=True, unrecoverable=attempt >= 1,
    )


def build_repair_prompt(errors: List[str]) -> str:
    """给模型的修复指令（只说问题，不透露内部实现）。"""
    return (
        "上一次输出不符合结构要求，请修正后重新输出 JSON。\n"
        f"具体问题：\n- " + "\n- ".join(errors) + "\n"
        "要求：只输出 JSON，不要任何解释文字。decision 必须是 "
        "apply_now / optimize_first / collect_evidence / wait / skip 之一。"
    )
