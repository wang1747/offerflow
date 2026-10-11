import hashlib
import re
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.models.jd import JobDescription
from app.models.match import MatchResult
from app.models.resume import Resume
from app.core.vector_store import get_vector_store, get_or_create_collection
from app.core.embeddings import embed_text, embed_hash_fallback
from app.core.text_utils import SKILL_ALIASES, extract_skills as _extract_skills, normalize_text as _normalize

_SEMANTIC_COLLECTION = "jd_resume_matches_v2"

# ============ 本地技能词典匹配（保留作为降级方案） ============


def _skill_sources(jd: JobDescription, resume: Resume) -> tuple:
    jd_parts = [
        jd.raw_text or "",
        " ".join(jd.must_have or []),
        " ".join(jd.nice_to_have or []),
        " ".join(
            value
            for values in (jd.tech_stack or {}).values()
            if isinstance(values, list)
            for value in values
        ),
    ]
    resume_parsed = resume.parsed_json or {}
    resume_parts = [
        resume.raw_text or "",
        " ".join(resume_parsed.get("skills", []) if isinstance(resume_parsed.get("skills"), list) else []),
    ]
    return " ".join(jd_parts), " ".join(resume_parts)


def _jd_embed_text(jd: JobDescription) -> str:
    """JD 侧：核心信息前置，must_have 重复加权"""
    parts = [
        f"职位：{jd.position or ''} 公司：{jd.company or ''}",
        "必备技能：" + "、".join(jd.must_have or []),
        "必备技能：" + "、".join(jd.must_have or []),
        "加分技能：" + "、".join(jd.nice_to_have or []),
        "职责要求：" + (jd.raw_text or "")[:600],
    ]
    return "\n".join(p for p in parts if p.strip())


def _resume_embed_text(resume: Resume) -> str:
    """简历侧：优先用解析后的结构化字段，raw_text 只做补充"""
    parsed = resume.parsed_json or {}
    skills = parsed.get("skills", []) if isinstance(parsed.get("skills"), list) else []
    parts = [
        "技能：" + "、".join(skills),
        (resume.raw_text or "")[:800],
    ]
    return "\n".join(p for p in parts if p.strip())


def _extract_skill_aliases(term: str) -> List[str]:
    """返回某个技能词可能命中的别名"""
    normalized = _normalize(term)
    return [normalized] + [alias for aliases in SKILL_ALIASES.values() for alias in aliases if alias in normalized]


def _persist_match(
    jd_id: str,
    resume_id: str,
    score: int,
    matched: List[str],
    missing: List[str],
    partial: List[str],
    suggestion: str,
    db: Session,
) -> MatchResult:
    existing = db.query(MatchResult).filter(
        MatchResult.jd_id == jd_id,
        MatchResult.resume_id == resume_id,
    ).first()
    if existing:
        existing.score = score
        existing.skill_match_detail = {"matched": matched, "missing": missing, "partial": partial}
        existing.gap_analysis = {"hard_gap": missing, "soft_gap": partial, "suggestion": suggestion}
        existing.suggestion = suggestion
        db.commit()
        db.refresh(existing)
        return existing
    match = MatchResult(
        jd_id=jd_id,
        resume_id=resume_id,
        score=score,
        skill_match_detail={"matched": matched, "missing": missing, "partial": partial},
        gap_analysis={"hard_gap": missing, "soft_gap": partial, "suggestion": suggestion},
        suggestion=suggestion,
    )
    db.add(match)
    db.commit()
    db.refresh(match)
    return match


# ============ 语义匹配（ChromaDB 增强） ============

_jd_embed_cache: Dict[str, tuple] = {}  # jd_id -> (text_hash, embedding)


def _get_jd_embedding(jd_id: str, jd_text: str):
    """带缓存的 JD embedding，避免排行榜场景重复计算"""
    h = hashlib.md5(jd_text.encode("utf-8")).hexdigest()
    cached = _jd_embed_cache.get(jd_id)
    if cached and cached[0] == h:
        return cached[1]
    vec = embed_text(jd_text) or embed_hash_fallback(jd_text)
    _jd_embed_cache[jd_id] = (h, vec)
    return vec


def _semantic_score(jd_id: str, jd_text: str, resume_text: str) -> int:
    """将 JD 向量写入 ChromaDB，再用简历向量做余弦相似度查询"""
    try:
        client = get_vector_store()
        collection = get_or_create_collection(
            client,
            _SEMANTIC_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        collection.upsert(
            ids=[jd_id],
            embeddings=[_get_jd_embedding(jd_id, jd_text)],
            metadatas=[{"jd_id": jd_id}],
        )

        resume_embedding = embed_text(resume_text) or embed_hash_fallback(resume_text)
        results = collection.query(
            query_embeddings=[resume_embedding],
            n_results=1,
            where={"jd_id": jd_id},
            include=["distances"],
        )

        if results.get("ids") and results["ids"] and results["ids"][0]:
            distance = results["distances"][0][0]
            similarity = max(0.0, min(1.0, 1.0 - distance))
            return int(round(similarity * 100))
        return 0
    except Exception as e:
        print(f"[WARN] 语义匹配失败，使用本地匹配: {e}")
        return 0


# ============ 主匹配函数（混合方案） ============

def calculate_match(jd_id: str, resume_id: str, db: Session, user_id: str) -> dict:
    """
    计算 JD 与简历的匹配度
    优先使用 ChromaDB 语义匹配，降级使用本地技能词典
    """
    jd = db.query(JobDescription).filter(
        JobDescription.id == jd_id,
        JobDescription.user_id == user_id,
    ).first()
    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id,
    ).first()
    if not jd:
        raise ValueError("JD 不存在")
    if not resume:
        raise ValueError("简历不存在")

    jd_text, resume_text = _skill_sources(jd, resume)

    # 1. 本地技能词典匹配
    jd_skills = _extract_skills(jd_text)
    resume_skills = _extract_skills(resume_text)

    matched = sorted(jd_skills & resume_skills)
    missing = sorted(jd_skills - resume_skills)
    partial = []
    for item in (jd.nice_to_have or []):
        if item and not any(alias in _normalize(resume_text) for alias in _extract_skill_aliases(item)):
            partial.append(item)

    hard_weight = 12
    nice_weight = 4
    local_score = max(0, min(100, 100 - len(missing) * hard_weight - len(partial) * nice_weight))

    # 2. 语义匹配（ChromaDB，结构化文本）
    semantic_score = _semantic_score(
        jd.id, _jd_embed_text(jd), _resume_embed_text(resume)
    )

    # 3. 语义分定领域基准，硬技能缺口做扣减
    if semantic_score > 0:
        SEMANTIC_FLOOR, SEMANTIC_CEIL = 45.0, 85.0
        semantic_pct = max(
            0.0,
            min(
                1.0,
                (semantic_score - SEMANTIC_FLOOR) / (SEMANTIC_CEIL - SEMANTIC_FLOOR),
            ),
        )
        domain_score = 40 + semantic_pct * 35
        penalty = min(35, len(missing) * 8 + len(partial) * 2)
        final_score = int(round(max(0, domain_score - penalty)))
    else:
        final_score = local_score

    suggestion = (
        "匹配度较高，可以直接投递。"
        if final_score >= 80
        else "建议补充缺失技能或相关项目经验后再投递。"
    )

    match = _persist_match(
        jd_id=jd_id,
        resume_id=resume_id,
        score=final_score,
        matched=matched,
        missing=missing,
        partial=partial,
        suggestion=suggestion,
        db=db,
    )
    return {
        "id": match.id,
        "jd_id": jd_id,
        "resume_id": resume_id,
        "company": jd.company,
        "position": jd.position,
        "score": match.score,
        "skill_match_detail": match.skill_match_detail,
        "gap_analysis": match.gap_analysis,
        "suggestion": match.suggestion,
        "created_at": match.created_at,
    }


def get_match_detail(match_id: str, db: Session, user_id: str) -> Optional[dict]:
    """获取单条匹配结果详情"""
    match = db.query(MatchResult).join(
        JobDescription, MatchResult.jd_id == JobDescription.id
    ).filter(
        MatchResult.id == match_id,
        JobDescription.user_id == user_id,
    ).first()
    if not match:
        return None
    return {
        "id": match.id,
        "jd_id": match.jd_id,
        "resume_id": match.resume_id,
        "score": match.score,
        "skill_match_detail": match.skill_match_detail,
        "gap_analysis": match.gap_analysis,
        "suggestion": match.suggestion,
        "created_at": match.created_at,
    }


def get_rankings(db: Session, user_id: str, page: int = 1, page_size: int = 20) -> List[dict]:
    """获取当前用户按匹配度排序的 JD 列表"""
    query = db.query(MatchResult).join(
        JobDescription, MatchResult.jd_id == JobDescription.id
    ).filter(
        JobDescription.user_id == user_id
    ).order_by(MatchResult.score.desc())
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return [
        {
            "id": row.id,
            "jd_id": row.jd_id,
            "resume_id": row.resume_id,
            "company": row.jd.company,
            "position": row.jd.position,
            "score": row.score,
            "suggestion": row.suggestion,
            "created_at": row.created_at,
        }
        for row in rows
    ]
