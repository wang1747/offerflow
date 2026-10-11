"""简历领域**公开只读能力**（Agent 唯一允许的入口）。

设计依据：`docs/agent/v0-agent-capability-boundary.md` 第 4/5/17 节。

规矩（Agent 必须遵守，本文件负责强制）：
- 只能调用本模块的 ``*_capability`` 函数，**不得**导入 ``services`` 里的私有函数、
  也不得直接查询 ``resumes`` 表。
- 每个能力都校验 ``user_id`` 归属；跨用户一律返回 ``forbidden`` / ``not_found``，
  **绝不返回**别人的数据，也不返回"存在但无权限"这类可区分信息。
- **不返回完整简历正文**（``raw_text``）。只返回摘要、结构化字段、版本和引用，
  需要正文时由调用方显式走 ``resume.get_text``，且该能力不进 v0.2-alpha 首批目录。
- 不重新实现业务算法：本文件只做**参数校验 + 权限校验 + 字段裁剪 + 引用组装**。

返回结构统一为 dict，字段与设计文档 13.2 的 ``ToolResult.data`` 对齐；调用方
（ToolGateway）负责再包一层 ``ToolResult`` 信封。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.resume import Resume
from app.modules.resume.versioning import document_id

logger = logging.getLogger(__name__)

# 摘要正文最多回传多少字符。设计文档要求"不返回完整正文"，
# 这里给出一个明确上限，避免调用方误把大段正文塞进 Agent 上下文。
SUMMARY_MAX_CHARS = 400


def _iso(value: Any) -> Optional[str]:
    """把 datetime 转成 ISO 字符串；None/非 datetime 原样返回 None。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _clip(text: Optional[str], limit: int = SUMMARY_MAX_CHARS) -> str:
    """裁剪文本到指定长度并标注截断。"""
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def _structured_summary(resume: Resume) -> Dict[str, Any]:
    """从 ``parsed_json`` 提取结构化摘要。

    只取标题/技能/公司等**短字段**，不返回 bullets 全量正文——
    bullets 可能很长，会把 Agent 上下文撑爆。
    """
    pj = resume.parsed_json or {}
    generation = pj.get("generation") or pj.get("structured") or {}
    if not isinstance(generation, dict):
        generation = {}

    name = generation.get("name") or ""
    skills = generation.get("skills") or []
    if isinstance(skills, str):
        skills = [skills]

    experiences = generation.get("experiences") or []
    companies = []
    if isinstance(experiences, list):
        for item in experiences[:5]:  # 只取前 5 段经历的公司名
            if isinstance(item, dict):
                company = item.get("company") or item.get("organization") or ""
                if company:
                    companies.append(company)

    return {
        "name": name,
        "skills": [s for s in skills if s][:30],
        "companies": companies,
        # 有无结构化数据：Agent 据此判断是否需要提示用户先解析
        "has_structured": bool(generation),
    }


def _resume_ref(resume: Resume, doc_id: Optional[str]) -> Dict[str, Any]:
    """统一的业务对象引用（含版本），供 evidence 与 object_refs 使用。"""
    return {
        "type": "resume",
        "id": resume.id,
        "version": resume.version,
        "document_id": doc_id or resume.id,
        "updated_at": _iso(resume.created_at),
    }


# ---------------------------------------------------------------------------
# 能力 1：resume.list —— 用于上下文选择，不返回完整正文
# ---------------------------------------------------------------------------
def list_resumes_capability(
    db: Session,
    user_id: str,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """列出用户的简历摘要（按 document_id 聚合，只取最新版本）。

    设计文档 5.1：``limit`` / ``cursor`` → 简历 ID、标题、版本、摘要。
    """
    from app.modules.resume.services import list_resumes

    limit = max(1, min(int(limit or 20), 100))
    rows = list_resumes(db, user_id)

    # list_resumes 已按 created_at 倒序；cursor 语义为"从该 id 之后继续"
    if cursor:
        ids = [r["id"] for r in rows]
        if cursor in ids:
            rows = rows[ids.index(cursor) + 1:]

    page = rows[:limit]
    items: List[Dict[str, Any]] = []
    for row in page:
        resume = db.query(Resume).filter(
            Resume.id == row["id"],
            Resume.user_id == user_id,
        ).first()
        if not resume:
            continue
        items.append({
            "id": resume.id,
            "document_id": row.get("document_id") or resume.id,
            "version": resume.version,
            "version_count": row.get("version_count", 1),
            "source_file": resume.source_file,
            "summary": _structured_summary(resume),
            "preview": _clip(resume.raw_text, 160),
            "updated_at": _iso(resume.created_at),
        })

    return {
        "items": items,
        "next_cursor": page[-1]["id"] if len(rows) > limit and page else None,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# 能力 2：resume.get —— 元数据 + 结构化摘要 + 版本 + 引用
# ---------------------------------------------------------------------------
def get_resume_capability(db: Session, user_id: str, resume_id: str) -> Dict[str, Any]:
    """读取单份简历的摘要与版本信息。

    跨用户返回 ``{"status": "not_found"}``——**不区分"不存在"和"无权限"**，
    避免通过状态码探测他人简历是否存在。
    """
    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()
    if not resume:
        return {"status": "not_found", "message": "简历不存在"}

    # document_id 需要全量行来做父链回溯，这里只查该文档的行
    all_rows = db.query(Resume).filter(Resume.user_id == user_id).all()
    mapping = {row.id: row for row in all_rows}
    doc_id = document_id(resume, mapping)

    return {
        "status": "succeeded",
        "resume": {
            "id": resume.id,
            "document_id": doc_id,
            "version": resume.version,
            "source_file": resume.source_file,
            "summary": _structured_summary(resume),
            # 只给截断预览；完整正文不外泄
            "preview": _clip(resume.raw_text),
            "word_count": len((resume.raw_text or "").strip()),
            "updated_at": _iso(resume.created_at),
        },
        "object_refs": [_resume_ref(resume, doc_id)],
        "object_version": f"resume:{resume.id}:v{resume.version}",
    }
