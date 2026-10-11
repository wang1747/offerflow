"""匹配领域**公开只读能力**（Agent 唯一允许的入口）。

设计依据：`docs/agent/v0-agent-capability-boundary.md` 第 4/5/13.4/17 节。

规矩（与 resume/jd 的 capabilities 一致）：
- 只能调用本模块的 ``*_capability`` 函数，不得导入 ``services`` 私有函数、
  也不得直接查询 ``match_results`` 表。
- 校验 ``user_id`` 归属；跨用户统一返回 ``not_found``。
- **优先读取已有匹配结果，绝不重复计算**（设计文档 5.1 明确要求）：
  重新计算会走 Chroma 语义匹配，既慢又会撞平台 token 配额，
  且会让"同一份简历+JD 的分数"随时间漂移，破坏 Agent 证据的可追溯性。
  没有已有结果时返回 ``status="not_found"``，由 Agent 决定是否提示用户先跑匹配分析。

数据结构映射（来自 ``match/services.py::_persist_match``）：
- ``skill_match_detail`` = ``{matched, missing, partial}``
- ``gap_analysis`` = ``{hard_gap, soft_gap, suggestion}``
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.jd import JobDescription
from app.models.match import MatchResult
from app.models.resume import Resume

logger = logging.getLogger(__name__)

# 技能条目最多回传项数（单个 JD 技能表可能很长）
MAX_SKILLS = 40


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out = []
        for item in value:
            if item is None:
                continue
            text = item if isinstance(item, str) else str(item)
            if text.strip():
                out.append(text.strip())
        return out[:MAX_SKILLS]
    return [str(value)][:MAX_SKILLS]


def _load_match(db: Session, user_id: str, jd_id: str, resume_id: str) -> Optional[MatchResult]:
    """按 (jd_id, resume_id) 取已有匹配结果，并**同时校验两侧归属**。

    匹配表本身没有 ``user_id``，所以必须 join JD 与简历确认属于当前用户，
    否则会出现"用别人的 JD + 我的简历"这类越权读取。
    """
    return db.query(MatchResult).join(
        JobDescription, MatchResult.jd_id == JobDescription.id
    ).join(
        Resume, MatchResult.resume_id == Resume.id
    ).filter(
        MatchResult.jd_id == jd_id,
        MatchResult.resume_id == resume_id,
        JobDescription.user_id == user_id,
        Resume.user_id == user_id,
    ).first()


# ---------------------------------------------------------------------------
# 能力：match.get —— 读已有匹配结果，不重复计算
# ---------------------------------------------------------------------------
def get_match_capability(
    db: Session,
    user_id: str,
    resume_id: str,
    jd_id: str,
) -> Dict[str, Any]:
    """读取一份简历与一个 JD 的已有匹配结果。

    返回：匹配分、亮点（matched）、硬缺口（missing/hard_gap）、
    软缺口（partial/soft_gap）、建议，以及双方对象引用与版本。

    没有已有结果时返回 ``status="not_found"`` + ``reason="no_match_result"``，
    **不触发重新计算**（避免慢查询 + 配额消耗 + 分数漂移）。
    """
    match = _load_match(db, user_id, jd_id, resume_id)
    if not match:
        return {
            "status": "not_found",
            "reason": "no_match_result",
            "message": "尚未计算该简历与 JD 的匹配结果",
        }

    detail = match.skill_match_detail or {}
    gap = match.gap_analysis or {}
    if not isinstance(detail, dict):
        detail = {}
    if not isinstance(gap, dict):
        gap = {}

    matched = _as_list(detail.get("matched"))
    missing = _as_list(detail.get("missing"))
    partial = _as_list(detail.get("partial"))
    # gap_analysis 与 skill_match_detail 语义重叠，取并集兜底（历史数据可能只填其一）
    hard_gap = _as_list(gap.get("hard_gap")) or missing
    soft_gap = _as_list(gap.get("soft_gap")) or partial
    suggestion = gap.get("suggestion") or match.suggestion or ""

    jd = db.query(JobDescription).filter(
        JobDescription.id == jd_id,
        JobDescription.user_id == user_id,
    ).first()
    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()

    refs: List[Dict[str, Any]] = []
    if jd:
        refs.append({
            "type": "jd",
            "id": jd.id,
            "version": None,
            "updated_at": _iso(jd.created_at),
        })
    if resume:
        refs.append({
            "type": "resume",
            "id": resume.id,
            "version": resume.version,
            "updated_at": _iso(resume.created_at),
        })

    return {
        "status": "succeeded",
        "match": {
            "id": match.id,
            "score": match.score,
            "highlights": matched,        # 已具备的技能
            "hard_gaps": hard_gap,        # 必备但缺失
            "soft_gaps": soft_gap,        # 加分项缺失
            "suggestion": suggestion,
            "computed_at": _iso(match.created_at),
        },
        "evidence_refs": [
            f"match:{match.id}:score",
            f"match:{match.id}:highlights",
            f"match:{match.id}:gaps",
        ],
        "object_refs": refs,
        "object_version": f"match:{match.id}",
    }
