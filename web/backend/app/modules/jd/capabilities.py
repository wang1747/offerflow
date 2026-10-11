"""JD 领域**公开只读能力**（Agent 唯一允许的入口）。

设计依据：`docs/agent/v0-agent-capability-boundary.md` 第 4/5/17 节。

规矩（与 resume/match 的 capabilities 一致）：
- 只能调用本模块的 ``*_capability`` 函数，不得导入 ``services`` 私有函数、
  也不得直接查询 ``job_descriptions`` 表。
- 每个能力校验 ``user_id`` 归属；跨用户统一返回 ``not_found``（不泄露存在性）。
- **不返回完整 JD 正文**（``raw_text``）。返回结构化要求（must_have/nice_to_have/
  tech_stack/hidden_signals）+ 截断预览。
- 不重新实现业务算法：JD 解析由 JD 模块负责，本文件只做校验 + 裁剪 + 组装。

注意：JD 表**没有 version 字段**（不像简历有 document_id/version）。Agent 的
"对象版本"用 ``updated_at``（created_at）作为版本代理，并在 ``object_version``
里显式标注 ``noversion``，避免下游误以为存在可比较的版本号——设计文档要求
"上下文必须记录来源、版本、时间和可信度"，此处诚实标注比伪造版本号更安全。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.jd import JobDescription

logger = logging.getLogger(__name__)

# JD 正文预览上限（与 resume capabilities 同量级）
PREVIEW_MAX_CHARS = 400
# 结构化要求里单个列表最多回传的项数，防止超长 JD 撑爆上下文
MAX_ITEMS = 30


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _clip(text: Optional[str], limit: int = PREVIEW_MAX_CHARS) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def _as_list(value: Any) -> List[str]:
    """把 JSON 字段（可能是 list / str / None）统一成字符串列表。"""
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
        return out[:MAX_ITEMS]
    return [str(value)][:MAX_ITEMS]


def _requirements(jd: JobDescription) -> Dict[str, Any]:
    """提取结构化要求。JD 模块解析后的字段本身就是结构化的，直接裁剪即可。"""
    must = _as_list(jd.must_have)
    nice = _as_list(jd.nice_to_have)
    tech = _as_list(jd.tech_stack)
    return {
        "must_have": must,
        "nice_to_have": nice,
        "tech_stack": tech,
        # hidden_signals 可能包含对候选人的隐性要求，Agent 需要它来判断匹配度，
        # 但只作为结构化信号返回，不外泄推理过程。
        "hidden_signals": _as_list(jd.hidden_signals),
        "counts": {
            "must_have": len(must),
            "nice_to_have": len(nice),
            "tech_stack": len(tech),
        },
    }


def _jd_ref(jd: JobDescription) -> Dict[str, Any]:
    return {
        "type": "jd",
        "id": jd.id,
        "version": None,  # JD 表无 version 字段
        "updated_at": _iso(jd.created_at),
    }


# ---------------------------------------------------------------------------
# 能力 1：jd.list —— 用于选择目标岗位
# ---------------------------------------------------------------------------
def list_jds_capability(
    db: Session,
    user_id: str,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """列出用户的 JD 摘要（按创建时间倒序）。"""
    from app.modules.jd.services import list_jds

    limit = max(1, min(int(limit or 20), 100))
    rows = list_jds(db, user_id)  # 已按 created_at.desc() 排序

    if cursor:
        ids = [r.id for r in rows]
        if cursor in ids:
            rows = rows[ids.index(cursor) + 1:]

    page = rows[:limit]
    items: List[Dict[str, Any]] = []
    for jd in page:
        req = _requirements(jd)
        items.append({
            "id": jd.id,
            "company": jd.company or "",
            "position": jd.position or "",
            "preview": _clip(jd.raw_text, 160),
            "requirements_summary": {
                "must_have": req["must_have"][:10],
                "tech_stack": req["tech_stack"][:10],
                "counts": req["counts"],
            },
            "has_parsed": bool(req["must_have"] or req["tech_stack"]),
            "updated_at": _iso(jd.created_at),
        })

    return {
        "items": items,
        "next_cursor": page[-1].id if len(rows) > limit and page else None,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# 能力 2：jd.get —— 结构化要求 + 引用（不返回完整正文）
# ---------------------------------------------------------------------------
def get_jd_capability(db: Session, user_id: str, jd_id: str) -> Dict[str, Any]:
    """读取单个 JD 的结构化要求与引用。

    跨用户返回 ``{"status": "not_found"}``，不区分"不存在"与"无权限"。
    """
    jd = db.query(JobDescription).filter(
        JobDescription.id == jd_id,
        JobDescription.user_id == user_id,
    ).first()
    if not jd:
        return {"status": "not_found", "message": "JD 不存在"}

    req = _requirements(jd)
    return {
        "status": "succeeded",
        "jd": {
            "id": jd.id,
            "company": jd.company or "",
            "position": jd.position or "",
            "requirements": req,
            "preview": _clip(jd.raw_text),
            "word_count": len((jd.raw_text or "").strip()),
            "has_embedding": bool(jd.embedding_id),
            "updated_at": _iso(jd.created_at),
        },
        "object_refs": [_jd_ref(jd)],
        # JD 无 version 列，显式标注而非伪造版本号
        "object_version": f"jd:{jd.id}:noversion",
    }
