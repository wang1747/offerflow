import hashlib
import logging
import re
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.tools.ats_checker import check_ats_compatibility
from app.core.concurrency import run_sync
from app.core.text_utils import extract_skills
from app.models.application import Application
from app.models.jd import JobDescription
from app.models.match import MatchResult
from app.models.resume import Resume
from app.models.user import User
from app.modules.resume.optimizer import (
    JDRequirement,
    optimize_resume_text,
)
from app.modules.resume.versioning import (
    create_next_version,
    document_id,
    get_document_versions,
    same_document,
)

logger = logging.getLogger(__name__)

# 导出时的「惰性结构化提取」结果缓存：键 = (简历 id, 正文 md5)。
# 只在内存中缓存，**不写库** —— 导出是只读动作，绝不能改到历史版本。
_STRUCTURED_CACHE: dict = {}
_STRUCTURED_CACHE_MAX = 128


def _summarize_resume(raw_text: str) -> dict:
    """从简历文本提取结构化摘要，供匹配和展示使用。"""
    section_names = ["教育", "项目", "经验", "技能", "经历", "自我评价"]
    sections = [name for name in section_names if name in (raw_text or "")]
    return {
        "sections": sections,
        "skills": sorted(extract_skills(raw_text or "")),
        "has_contact": bool(
            re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw_text or "")
            or re.search(r"1[3-9]\d{9}", raw_text or "")
        ),
    }


def list_resumes(db: Session, user_id: str) -> list:
    """获取简历列表：**按 document_id 聚合**，每个文档只展示最新版本。

    - 新数据（有 ``document_id``）：直接按它分组；
    - 历史数据（只有 ``parent_id``）：沿父链回溯到根，等价于"同一份简历"；
    - 两份互不相关的简历天然是两个文档，各自从 v1 起算。
    """
    all_rows = db.query(Resume).filter(Resume.user_id == user_id).all()
    mapping = {row.id: row for row in all_rows}
    visible = [row for row in all_rows if (row.raw_text or "").strip()]

    groups: dict = {}  # document_id -> (latest, count)
    for row in visible:
        key = document_id(row, mapping)
        if key not in groups:
            groups[key] = (row, 1)
        else:
            prev, count = groups[key]
            groups[key] = ((row if (row.version or 0) > (prev.version or 0) else prev), count + 1)

    result = []
    for key, (latest, count) in groups.items():
        result.append({
            "id": latest.id,
            "version": latest.version,
            "raw_text": latest.raw_text,
            "source_file": latest.source_file,
            "parsed_json": latest.parsed_json,
            "created_at": latest.created_at,
            "version_count": count,
            "document_id": key,
        })
    result.sort(key=lambda x: (x["created_at"] or ""), reverse=True)
    return result


def get_resume_versions(resume_id: str, db: Session, user_id: str):
    """获取同一份简历（同一 document）的所有版本，按版本号倒序。"""
    doc = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()
    if not doc:
        return None
    return get_document_versions(db, user_id, doc)


def upload_resume(
    raw_text: str,
    source_file: Optional[str],
    db: Session,
    user_id: str,
    client_id: Optional[str] = None,
    parsed_extra: Optional[dict] = None,
) -> Resume:
    """上传并保存简历，拒绝空内容。

    **新上传 = 一个全新的 document**：``version`` 恒为 1、``document_id`` 指向自己。
    不再按用户全局递增版本号 —— 那样会把两份互不相关的简历串成"同一份的 v1/v2"。

    client_id：客户端自生成 id；同用户同 id 已存在则**直接返回**（幂等），
    供桌面版离线队列重复上行时不产生重复记录。
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("简历内容为空，无法上传")

    if client_id:
        existing = db.query(Resume).filter(
            Resume.id == client_id,
            Resume.user_id == user_id,
        ).first()
        if existing:
            return existing

    resume_id = client_id or str(uuid.uuid4())
    payload = _summarize_resume(text)
    payload["document_id"] = resume_id
    payload.update(parsed_extra or {})

    resume = Resume(
        id=resume_id,
        user_id=user_id,
        version=1,
        raw_text=text,
        parsed_json=payload,
        source_file=source_file,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


def _build_jd_requirement(jd: Optional[JobDescription], jd_text: str) -> JDRequirement:
    """优先用结构化 JD，否则回退到原始 JD 文本。"""
    if jd is not None:
        tech_stack: list = []
        ts = jd.tech_stack or {}
        if isinstance(ts, dict):
            for values in ts.values():
                if isinstance(values, list):
                    tech_stack.extend(values)
        elif isinstance(ts, list):
            tech_stack = ts

        return JDRequirement(
            raw_text=jd.raw_text or jd_text or "",
            company=jd.company or "",
            position=jd.position or "",
            must_have=jd.must_have or [],
            nice_to_have=jd.nice_to_have or [],
            tech_stack=tech_stack,
            hidden_signals=jd.hidden_signals or [],
        )

    return JDRequirement(raw_text=(jd_text or "").strip())


def save_optimized_version(
    resume_id: str,
    optimized_text: str,
    changes: list,
    target_jd: dict,
    added_keywords: list,
    db: Session,
    user_id: str,
    structured: Optional[dict] = None,
) -> Resume:
    """把优化结果保存为简历**新版本**（追加，不修改原版本）。"""
    source = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()
    if not source:
        raise ValueError("简历不存在")

    parsed_json = {
        "kind": "optimized",
        "changes": changes or [],
        "target_jd": target_jd or {},
        "added_keywords": added_keywords or [],
        "skills": sorted(extract_skills(optimized_text or "")),
    }
    source_payload = source.parsed_json if isinstance(source.parsed_json, dict) else {}
    if source_payload.get("source_photo"):
        parsed_json["source_photo"] = source_payload["source_photo"]
    # 结构化数据（用于导出保留层次），提取失败则跳过
    if structured:
        parsed_json["structured"] = structured

    return create_next_version(
        db,
        user_id,
        source,
        raw_text=optimized_text,
        parsed_json=parsed_json,
        source_file=source.source_file,
    )


def rollback_to_version(
    resume_id: str,
    target_version_id: str,
    db: Session,
    user_id: str,
) -> Resume:
    """把指定旧版本恢复为当前最新版本（对标标书模板的 rollbackToRevision）。

    回滚**不是**把旧记录改回"当前版本"，而是**复制目标版本内容追加一条新的最高版本**，
    并在 ``parsed_json.rolled_back_from`` 记录来源版本号 —— 历史一个都不少。
    跨文档（不同 document_id）回滚直接拒绝。
    """
    source = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()
    target = db.query(Resume).filter(
        Resume.id == target_version_id,
        Resume.user_id == user_id,
    ).first()
    if not source or not target:
        raise ValueError("简历不存在")

    # 必须属于同一份简历（同一 document）
    if not same_document(db, user_id, source, target):
        raise ValueError("所选版本不属于同一份简历")

    versions = get_document_versions(db, user_id, source)
    latest = versions[0] if versions else source

    return create_next_version(
        db,
        user_id,
        latest,
        raw_text=target.raw_text,
        parsed_json=dict(target.parsed_json or {}),
        source_file=target.source_file,
        rolled_back_from=target.version,
    )


async def optimize_resume_flow(
    resume_id: str,
    jd_text: str,
    db: Session,
    user: User,
    jd_id: Optional[str] = None,
) -> dict:
    """简历优化业务编排：加载原简历 -> 取目标 JD -> 优化 -> 保存新版本。

    支持两种 JD 输入：
    - jd_id：从数据库取结构化 JD（含 must_have/nice_to_have/tech_stack）；
    - jd_text：手动粘贴的 JD 原文。
    """
    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user.id,
    ).first()
    if not resume:
        raise ValueError("resume_not_found")

    # 解析目标 JD
    jd: Optional[JobDescription] = None
    if jd_id:
        jd = db.query(JobDescription).filter(
            JobDescription.id == jd_id,
            JobDescription.user_id == user.id,
        ).first()
        if not jd:
            raise ValueError("jd_not_found")
    requirement = _build_jd_requirement(jd, jd_text or "")

    target_jd = {
        "id": jd.id if jd else None,
        "company": requirement.company,
        "position": requirement.position,
    }

    result = await optimize_resume_text(resume.raw_text, requirement, user, db=db)
    if result.error:
        return {"success": False, "data": None, "error": result.error}

    new_version = None
    if result.optimized_text and result.optimized_text.strip():
        # 结构化提取（供导出保留层次），失败不阻塞优化主流程
        structured = None
        try:
            from app.modules.resume.optimizer.structure_extract import extract_structured
            structured = await run_sync(extract_structured, result.optimized_text, user)
        except Exception as e:
            logger.warning("优化结果结构化提取失败: %s", e)

        new_version = save_optimized_version(
            resume_id,
            result.optimized_text,
            [c.__dict__ for c in result.changes],
            target_jd,
            result.added_keywords,
            db,
            user.id,
            structured=structured,
        )

    return {
        "success": True,
        "data": {
            "optimized": result.optimized_text,
            "changes": [c.__dict__ for c in result.changes],
            "added_keywords": result.added_keywords,
            "removed_keywords": result.removed_keywords,
            "removed": result.removed,
            "length_warning": result.length_warning,
            "gap": {
                "matched": result.gap.matched,
                "missing": result.gap.missing,
                "partial": result.gap.partial,
            },
            "target_jd": target_jd,
            "preservation": {
                "score": result.preservation.score,
                "passed": result.preservation.passed,
                "fallback": result.preservation.fallback,
                "missing_facts": result.preservation.missing_facts,
                "added_facts": result.preservation.added_facts,
                "critical_facts": result.preservation.critical_facts,
            },
            "edits_applied": result.edit_count,
            "ats": check_ats_compatibility(resume.raw_text, requirement.raw_text),
            "ats_after": check_ats_compatibility(result.optimized_text, requirement.raw_text),
            "new_version": {
                "id": new_version.id,
                "version": new_version.version,
            } if new_version else None,
        },
        "error": None,
    }


def delete_resume(resume_id: str, db: Session, user_id: str) -> int:
    """删除指定简历的整个文档（含全部历史版本），返回删除条数。

    列表按 document_id 聚合，前端拿到的 id 是最新版本，因此删除语义也必须是
    “删除整份简历”；否则删掉 v2 后 v1 会重新出现，看起来像删除失败。
    """
    from app.modules.corpus.services import delete_corpus_by_source
    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()
    if not resume:
        return 0
    resume_ids = [item.id for item in get_document_versions(db, user_id, resume)]
    # 级联清理：删除关联的匹配结果，投递记录解除简历关联
    db.query(MatchResult).filter(
        MatchResult.resume_id.in_(resume_ids)
    ).delete(synchronize_session=False)
    db.query(Application).filter(
        Application.resume_id.in_(resume_ids),
        Application.user_id == user_id,
    ).update({"resume_id": None}, synchronize_session=False)
    deleted = db.query(Resume).filter(
        Resume.id.in_(resume_ids),
        Resume.user_id == user_id,
    ).delete(synchronize_session=False)
    db.commit()
    # 级联清理该文档各版本贡献到共享语料库的副本（若曾 AI 生成且同意贡献）
    for version_id in resume_ids:
        delete_corpus_by_source(version_id, item_type="resume", db=db)
    return deleted


def delete_all_resumes(db: Session, user_id: str) -> int:
    """删除当前用户的全部简历，返回删除条数。级联清理关联的匹配结果、投递引用与语料副本。"""
    from app.modules.corpus.services import delete_corpus_by_source
    resume_ids = [r.id for r in db.query(Resume).filter(
        Resume.user_id == user_id
    ).all()]
    if resume_ids:
        db.query(MatchResult).filter(
            MatchResult.resume_id.in_(resume_ids)
        ).delete(synchronize_session=False)
        db.query(Application).filter(
            Application.resume_id.in_(resume_ids),
            Application.user_id == user_id,
        ).update({"resume_id": None}, synchronize_session=False)
    deleted = db.query(Resume).filter(
        Resume.user_id == user_id
    ).delete(synchronize_session=False)
    db.commit()
    # 级联清理这些简历贡献的语料副本
    for rid in resume_ids:
        delete_corpus_by_source(rid, item_type="resume", db=db)
    return deleted


def ensure_resume_structured(resume: Resume, db: Session, user: User) -> Optional[dict]:
    """导出前惰性补结构化数据（供导出保留排版层次），**只算不写库**。

    背景：导出时 `export_service._generation_structured()` 只认 `parsed_json` 里的
    `generation`（生成版产出）或 `structured`（优化版产出）。上传/粘贴进来的简历
    只有一份摘要，导出会退化成**纯文本渲染**，标题行 / bullet / 时间对齐全丢。

    这里在导出时用 LLM 提取一次并**在内存里**返回给导出用；结果按
    (简历 id, 正文 md5) 在进程内缓存，同一份简历第二次导出不再花钱。

    ⚠️ **绝不写库**：导出是只读动作。以前这里会把 ``structured`` 回写进
    ``resume.parsed_json``，等于"用户只是看了一眼导出，历史版本就被改写了"，
    与"版本不可变"直接冲突。宁可每次重新提取（有缓存兜底），也不改历史。

    任何失败（未配置模型、额度不足、LLM 异常）都返回 None，
    **绝不影响导出本身** —— 大不了这一次按纯文本导出，下次再试。
    """
    pj = resume.parsed_json or {}
    existing = pj.get("generation") or pj.get("structured")
    if existing:
        return None  # 已有结构化数据（生成版 / 优化版 / 之前补过）

    text = (resume.raw_text or "").strip()
    if not text:
        return None

    cache_key = (resume.id, hashlib.md5(text.encode("utf-8")).hexdigest())
    cached = _STRUCTURED_CACHE.get(cache_key)
    if cached:
        return cached

    try:
        from app.modules.resume.optimizer.structure_extract import extract_structured

        structured = extract_structured(text, user)
    except Exception as e:  # 含 QuotaExceededError：导出不该因为提取失败而失败
        logger.warning("导出前结构化提取失败（不影响导出）: %s", e)
        return None

    if not structured:
        return None

    if len(_STRUCTURED_CACHE) >= _STRUCTURED_CACHE_MAX:
        _STRUCTURED_CACHE.clear()
    _STRUCTURED_CACHE[cache_key] = structured
    return structured


def export_resume_data(db: Session, user_id: str) -> list:
    """导出当前用户的全部简历数据（JSON 结构，供下载）。"""
    resumes = db.query(Resume).filter(
        Resume.user_id == user_id
    ).order_by(Resume.version.asc()).all()
    return [
        {
            "id": r.id,
            "version": r.version,
            "raw_text": r.raw_text,
            "source_file": r.source_file,
            "parsed_json": r.parsed_json,
            "created_at": r.created_at,
        }
        for r in resumes
    ]
