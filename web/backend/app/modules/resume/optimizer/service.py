"""简历优化服务。

策略（v2）：结构化差距分析 + 整份重写 + 事实保真校验。

对比旧的「定点替换」：
- 不再依赖 LLM 逐字复现原文做字符串定位，杜绝「编辑静默丢失」；
- 先把 JD 结构化需求 + 差距分析喂给 LLM，让它做有依据的整份重写；
- 重写后做关键事实保留校验，失败则回退原文，保证不编造。
"""

import json
import logging
import re
from typing import List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.core.concurrency import ainvoke_llm
from app.core.llm import get_user_llm_or_raise
from app.core.exceptions import QuotaExceededError
from app.models.user import User
from app.observability.tracing import trace_operation

from .contracts import Change, GapAnalysis, JDRequirement, OptimizationResult, PreservationResult
from .gap import analyze_gap
from .prompts import CONDENSE_SYSTEM_PROMPT, SYSTEM_PROMPT, build_user_prompt
from .sections import preservation_result

logger = logging.getLogger(__name__)

# 一页硬约束的参数（字符数估算，宽松）
_MAX_EXPAND_RATIO = 1.15   # 相对原文的最大扩写比
_ONE_PAGE_MAX_CHARS = 1400  # 绝对字符数上限（超过基本超一页）
_MIN_CONDENSE_CHARS = 800   # 低于此长度不做扩写判断


def _clean_json_response(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    # 去掉可能存在的 BOM
    text = text.lstrip("\ufeff")
    return text.strip()


def _parse_changes(payload: dict) -> List[Change]:
    raw = payload.get("changes") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    changes: List[Change] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        changes.append(
            Change(
                section=str(item.get("section") or "").strip(),
                before=str(item.get("before") or "").strip(),
                after=str(item.get("after") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
            )
        )
    return changes


def _str_list(value) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _fallback(original: str, error: str = "") -> OptimizationResult:
    return OptimizationResult(
        optimized_text=original,
        changes=[],
        added_keywords=[],
        removed_keywords=[],
        gap=GapAnalysis(),
        edit_count=0,
        preservation=PreservationResult(score=1.0, passed=True, fallback=True),
        error=error,
    )


def _estimate_too_long(original: str, optimized: str) -> bool:
    """估算优化结果是否超出一页 A4（保守估计，用字符数）。"""
    o_len = len(original or "")
    n_len = len(optimized or "")
    # 明显扩写：比原文长 15% 以上，且绝对长度不短
    if o_len and n_len > o_len * _MAX_EXPAND_RATIO and n_len > _MIN_CONDENSE_CHARS:
        return True
    # 绝对超一页：超过字符数上限
    if n_len > _ONE_PAGE_MAX_CHARS:
        return True
    return False


async def _condense_to_one_page(text: str, user: User) -> Tuple[str, List[str]]:
    """用「三刀裁剪法」让 LLM 精简超长简历，返回 (精简文本, 删除内容说明)。"""
    llm = get_user_llm_or_raise(user)
    response = await ainvoke_llm(llm, [
        SystemMessage(content=CONDENSE_SYSTEM_PROMPT),
        HumanMessage(content=text),
    ])
    payload = json.loads(_clean_json_response(response.content))
    if isinstance(payload, dict):
        return (
            (payload.get("optimized_text") or "").strip(),
            _str_list(payload.get("removed")),
        )
    return "", []


async def optimize_resume_text(
    resume_text: str,
    jd: JDRequirement,
    user: User,
    db: Optional[Session] = None,
) -> OptimizationResult:
    """对简历做针对目标 JD 的整份重写优化。"""
    original = (resume_text or "").strip()
    if not original:
        return OptimizationResult(
            optimized_text="",
            error="原简历内容为空，请先上传可编辑文本的简历文件",
            preservation=PreservationResult(score=0.0, passed=False),
        )

    if not (jd.raw_text or "").strip() and not jd.must_have and not jd.tech_stack:
        return OptimizationResult(
            optimized_text=original,
            preservation=PreservationResult(score=1.0, passed=True),
            error="目标 JD 为空，已返回原简历",
        )

    # 1. 差距分析（确定性）
    gap = analyze_gap(original, jd)

    # 2. 调用 LLM 做整份重写
    try:
        with trace_operation("resume_optimize", getattr(user, "id", None)):
            llm = get_user_llm_or_raise(user)

            # RAG：检索语料库中最相似的同方向简历作为写法参考（失败静默降级）
            reference = ""
            try:
                from app.modules.corpus.services import retrieve_similar
                query = " ".join(filter(None, [jd.position, jd.company, original[:300]]))
                similar = retrieve_similar(
                    query,
                    top_k=2,
                    item_type="resume",
                    db=db,
                    viewer_user_id=getattr(user, "id", None),
                    include_private=True,
                )
                if similar:
                    reference = "\n\n".join(
                        f"—— 参考 {i + 1}（相似度 {s['score']:.2f}）——\n{s['raw_text'][:900]}"
                        for i, s in enumerate(similar)
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("语料检索失败（不影响优化）: %s", e)

            user_prompt = build_user_prompt(
                original,
                jd.to_prompt(),
                gap.matched,
                gap.missing,
                gap.partial,
                reference=reference,
            )
            response = await ainvoke_llm(llm, [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            payload = json.loads(_clean_json_response(response.content))
    except QuotaExceededError:
        raise
    except Exception as exc:
        logger.error("简历优化调用失败: %s", exc)
        return _fallback(original, "AI 优化调用失败，已保留原简历")

    if not isinstance(payload, dict):
        return _fallback(original, "AI 返回格式异常，已保留原简历")

    optimized = (payload.get("optimized_text") or "").strip()
    if not optimized:
        return _fallback(original, "AI 未返回优化内容，已保留原简历")

    # 3. 关键事实保留校验
    preservation = preservation_result(original, optimized)
    if not preservation.passed:
        logger.warning(
            "简历优化结果未通过内容保留校验，回退为原简历: missing=%s added=%s",
            preservation.missing_facts,
            preservation.added_facts,
        )
        if preservation.added_facts:
            return _fallback(original, "优化结果可能新增了原文没有的信息，已保留原简历")
        return _fallback(original, "优化结果可能丢失关键信息，已保留原简历")

    changes = _parse_changes(payload)
    added_keywords = _str_list(payload.get("added_keywords"))
    removed_keywords = _str_list(payload.get("removed_keywords"))
    removed = _str_list(payload.get("removed"))
    length_warning = ""

    # 4. 一页预算：超长时用「三刀裁剪法」精简，精简后仍超长则保留精简成果 + 提示，
    #    而不是粗暴回退原简历（把「取舍」留给用户判断）
    if _estimate_too_long(original, optimized):
        logger.warning("优化结果超出 A4 一页限制，用三刀裁剪法精简")
        try:
            with trace_operation("resume_condense", getattr(user, "id", None)):
                condensed, cond_removed = await _condense_to_one_page(optimized, user)
            if condensed:
                cond_preservation = preservation_result(original, condensed)
                if cond_preservation.passed:
                    optimized = condensed
                    preservation = cond_preservation
                    if cond_removed:
                        removed = cond_removed
        except Exception as exc:
            logger.warning("精简到一页失败: %s", exc)

        # 精简后仍超长：保留精简成果，明确提示用户还需自行删减
        if _estimate_too_long(original, optimized):
            length_warning = "简历内容较多，已尽量精简，仍可能略超一页，建议删减与目标岗位无关的经历"

    return OptimizationResult(
        optimized_text=optimized,
        changes=changes,
        added_keywords=added_keywords,
        removed_keywords=removed_keywords,
        removed=removed,
        length_warning=length_warning,
        gap=gap,
        edit_count=len(changes),
        preservation=preservation,
    )
