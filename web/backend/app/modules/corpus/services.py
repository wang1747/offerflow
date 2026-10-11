"""语料库服务：贡献入库 + 语义检索（RAG 的检索层）。

设计对齐 deploy/PLAN.md 的「语料库建设」：
  - 整篇 embedding（不切片，OfferFlow 语料是短文档：简历 800 字 / JD 千字 / 面经）
  - 语料写入 corpus_items（PG，SQLAlchemy）+ 向量存 ChromaDB（corpus_embeddings）
  - retrieve_similar 检索最相似语料，喂给 LLM 做参考
  - 分类：复用 skill_keywords 的真实 JD 词频库做方向标签；技能词打 tags
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.corpus import CorpusItem
from app.core.vector_store import get_vector_store, get_or_create_collection
from app.core.embeddings import embed_text, embed_hash_fallback
from app.core.skill_keywords import keyword_set
from app.core.hybrid import bm25_search, rrf_fuse

logger = logging.getLogger(__name__)

_CORPUS_COLLECTION = "corpus_embeddings"

# ---- 超长语料切块 ----
# bge-small-zh-v1.5 的上下文上限是 512 token，中文约 1 字≈1 token。
# 实测：>512 字的语料整篇 embedding 会被**静默截断**（前 512 字之后的
# 内容完全不进向量，召回永远找不到后半段的关键信息，且不报错）。
# 正解：> 阈值时切块 + 重叠窗口，每块独立向量；检索时聚合回原文。
_CHUNK_SIZE = 480      # 每块最大字数（留余量，避免边界 token 溢出 512）
_CHUNK_OVERLAP = 64    # 相邻块重叠字数（保留跨块语义）


# ============ 分类 ============

def classify_direction(text: str) -> Optional[str]:
    """用 19 大类求职方向关键词表（salary_benchmark）做粗分类，返回中文方向标签。

    0 命中返回 None（方向可空，检索时不强制过滤）。
    """
    from app.core.salary_benchmark import _DIRECTION_KEYWORDS, DIRECTION_COEF
    t = (text or "").lower()
    best_key, best_n = None, 0
    for key, kws in _DIRECTION_KEYWORDS:
        n = sum(1 for kw in kws if kw and kw.lower() in t)
        if n > best_n:
            best_key, best_n = key, n
    if not best_key:
        return None
    return DIRECTION_COEF.get(best_key, (0.0, best_key))[1]


def extract_tags(text: str, limit: int = 20) -> List[str]:
    """从文本中提取命中真实 JD 词频库的技能词作为 tags（供过滤/展示）。"""
    kws = keyword_set()
    if not kws:
        return []
    t = (text or "").lower()
    hits = sorted({kw for kw in kws if kw in t})
    return hits[:limit]


# ============ 入库 ============

def _chunk_text(text: str) -> List[str]:
    """把文本切成重叠块；<=_CHUNK_SIZE 时返回单元素列表（不切）。

    返回空列表仅当输入为空串（调用方需兜底）。
    """
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= _CHUNK_SIZE:
        return [t]
    chunks: List[str] = []
    start = 0
    while start < len(t):
        chunks.append(t[start:start + _CHUNK_SIZE])
        if start + _CHUNK_SIZE >= len(t):
            break
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


def _chunk_vector_id(item_id: str, index: int, total: int) -> str:
    """切块向量的 id：单块 = item_id（与旧数据兼容）；多块 = item_id#chunk{i}。"""
    if total == 1:
        return item_id
    return f"{item_id}#chunk{index}"


def _to_item_id(vector_id: str) -> str:
    """向量 id 反解回语料条目 id（切块向量带 #chunk 后缀）。"""
    return vector_id.split("#", 1)[0]


def _embed(text: str) -> List[float]:
    return embed_text(text) or embed_hash_fallback(text)


def add_corpus_item(
    item_type: str,
    raw_text: str,
    db: Session,
    structured: Optional[dict] = None,
    tags: Optional[List[str]] = None,
    source: str = "user_upload",
    user_id: Optional[str] = None,
    is_public: bool = False,
    auto_tag: bool = True,
    source_id: Optional[str] = None,
) -> CorpusItem:
    """写入一条语料：存 corpus_items（PG）+ embedding 存 Chroma。

    - source_id：关联的业务表 id（简历/JD/面经），删除业务数据时据此级联清理。
    - 超长文本（> _CHUNK_SIZE 字）切块逐块 embedding，向量 id 用 item_id#chunk{i}，
      metadata 里统一记 source_item_id=item_id，删除与聚合都靠它。
    - embedding 失败不阻断入库（语料仍在，只是暂不可语义检索）。
    """
    direction = classify_direction(raw_text)
    final_tags = list(tags or [])
    if auto_tag:
        final_tags = list(dict.fromkeys(final_tags + extract_tags(raw_text)))

    item = CorpusItem(
        item_type=item_type,
        raw_text=raw_text,
        structured=structured,
        tags=final_tags,
        direction=direction,
        source=source,
        source_id=source_id,
        user_id=user_id,
        is_public=is_public,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    try:
        chunks = _chunk_text(raw_text) or [raw_text or ""]
        total = len(chunks)
        ids = [_chunk_vector_id(item.id, i, total) for i in range(total)]
        embeddings = [_embed(c) for c in chunks]
        metadatas = [{
            "item_type": item_type,
            "direction": direction or "",
            "source": source,
            "source_item_id": item.id,
        } for _ in chunks]
        collection = get_or_create_collection(
            get_vector_store(),
            _CORPUS_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
        )
        item.embedding_id = item.id
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("语料 embedding 失败（不影响入库）: %s", e)

    return item


# ============ 检索 ============

def _apply_corpus_visibility(
    query,
    viewer_user_id: Optional[str] = None,
    include_private: bool = False,
    is_admin: bool = False,
):
    """可见性 = 公开语料 + 匿名导入语料 + 自己的私有语料；管理员看全部。

    `user_id` 为空的条目是**匿名导入的公开数据集**（爬虫 / 面经题库 / 简历模板，
    见 models/corpus.py 的说明），没有归属人 ⇒ 不存在隐私问题，必须与 `is_public`
    并列放行。

    回归背景：加固初期只放行 `is_public`，导致 346 条 `source='import'`
    （user_id 为空、is_public 默认 false）的匿名语料变成管理员专属 ——
    普通用户可见量从 356 条掉到 10 条；而桌面版本地索引正是靠
    `/corpus/export` 拉全量重建，会随之塌掉。用户私有语料仍严格隔离。
    """
    if is_admin:
        return query
    anonymously_shared = CorpusItem.user_id.is_(None)
    if include_private and viewer_user_id:
        return query.filter(or_(
            CorpusItem.is_public.is_(True),
            anonymously_shared,
            CorpusItem.user_id == viewer_user_id,
        ))
    return query.filter(or_(
        CorpusItem.is_public.is_(True),
        anonymously_shared,
    ))


def retrieve_similar(
    query_text: str,
    top_k: int = 5,
    item_type: Optional[str] = None,
    direction: Optional[str] = None,
    db: Optional[Session] = None,
    hybrid: bool = True,
    viewer_user_id: Optional[str] = None,
    include_private: bool = False,
    is_admin: bool = False,
) -> List[Dict]:
    """检索语料库中最相似的 N 条，返回结构化结果（含相似度）。

    默认走「向量 + BM25 混合检索（RRF 融合）」；BM25 关键词召回对技术术语更友好。
    hybrid=False 时回退到纯向量检索。db 传入时从 PG 取 structured 与原文。

    返回 [{id, item_type, raw_text, structured, tags, direction, score}]。
    """
    if not query_text or not query_text.strip() or db is None:
        return []

    # 召回池：多召回几路，给 RRF 融合留出重排空间
    pool = max(top_k, min(top_k * 5, 60))

    # ---- 1) 向量召回 ----
    vector_hits: List[tuple] = []          # [(id, similarity)]
    docs_by_id: Dict[str, str] = {}
    metas_by_id: Dict[str, dict] = {}
    try:
        collection = get_or_create_collection(
            get_vector_store(),
            _CORPUS_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        where = {}
        if item_type:
            where["item_type"] = item_type
        if direction:
            where["direction"] = direction
        results = collection.query(
            query_embeddings=[_embed(query_text)],
            n_results=pool,
            where=where or None,
            include=["metadatas", "distances", "documents"],
        )
        ids = (results.get("ids") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        documents = (results.get("documents") or [[]])[0]
        # 聚合：切块向量的 id 带 #chunk{i} 后缀，反解回语料条目 id 去重；
        # 同一条语料的多个 chunk 命中时只保留最高相似度。
        best: Dict[str, float] = {}
        for idx, cid in enumerate(ids):
            dist = distances[idx] if idx < len(distances) else 1.0
            sim = max(0.0, min(1.0, 1.0 - dist))
            base_id = _to_item_id(cid)
            if base_id not in best or sim > best[base_id]:
                best[base_id] = sim
            # documents/metadatas 兜底按条目 id 记（PG 缺失时才用）
            if base_id not in docs_by_id:
                docs_by_id[base_id] = documents[idx] if idx < len(documents) else ""
                metas_by_id[base_id] = metadatas[idx] if idx < len(metadatas) else {}
        vector_hits = sorted(best.items(), key=lambda kv: -kv[1])
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 向量检索失败: {e}")

    # ---- 2) BM25 关键词召回 ----
    bm25_hits: List[tuple] = []
    if hybrid and db is not None:
        try:
            bm25_hits = bm25_search(db, query_text, pool, item_type, direction)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] BM25 检索失败: {e}")

    # ---- 3) RRF 融合 ----
    vec_score = {cid: s for cid, s in vector_hits}
    if bm25_hits:
        fused = rrf_fuse(vector_hits, bm25_hits)
        candidate_ids = [cid for cid, _ in fused]
    else:
        candidate_ids = [cid for cid, _ in vector_hits]

    if not candidate_ids:
        return []
    candidate_ids = candidate_ids[:pool]
    rank_score = {cid: round(1.0 / (i + 1), 4) for i, cid in enumerate(candidate_ids)}

    # ---- 4) 从 PG 取原文/结构化（PG 是 source of truth）----
    rows_query = _apply_corpus_visibility(
        db.query(CorpusItem).filter(CorpusItem.id.in_(candidate_ids)),
        viewer_user_id=viewer_user_id,
        include_private=include_private,
        is_admin=is_admin,
    )
    rows = rows_query.all()
    items_by_id: Dict[str, CorpusItem] = {r.id: r for r in rows}
    ranked_ids = [cid for cid in candidate_ids if cid in items_by_id][:top_k]
    if not ranked_ids:
        return []

    out = []
    for cid in ranked_ids:
        row = items_by_id[cid]
        score = vec_score.get(cid, rank_score.get(cid, 0.0))
        out.append({
            "id": cid,
            "item_type": row.item_type,
            "raw_text": row.raw_text or "",
            "structured": row.structured,
            "tags": row.tags or [],
            "direction": row.direction,
            "score": round(float(score), 4),
        })
    return out


# ============ CRUD ============

def list_corpus(
    db: Session,
    item_type: Optional[str] = None,
    limit: Optional[int] = 50,
    viewer_user_id: Optional[str] = None,
    include_private: bool = False,
    is_admin: bool = False,
) -> List[CorpusItem]:
    """列出语料。

    limit=None 表示不限量（全量导出用，见 routes./export）；
    默认 50 保持与既有 /list 行为一致，不改变现有调用方语义。
    """
    q = db.query(CorpusItem)
    if item_type:
        q = q.filter(CorpusItem.item_type == item_type)
    q = _apply_corpus_visibility(
        q,
        viewer_user_id=viewer_user_id,
        include_private=include_private,
        is_admin=is_admin,
    )
    q = q.order_by(CorpusItem.created_at.desc())
    if limit is not None:
        q = q.limit(limit)
    return q.all()


def _delete_embeddings(item_id: str) -> None:
    """删除某条语料的全部向量（含切块），失败只记日志、不抛异常。

    新旧数据都兼容：
    - 新数据（单块 + 切块）：metadata 带 source_item_id=item_id，用 where 删
    - 旧数据（无 source_item_id）：向量 id 就是 item_id，用 ids 兜底删
    之所以不静默吞掉：之前 `except Exception: pass` 让向量删除失败无声无息，
    长期累积孤儿向量占召回名额、又查不出原因。现在至少留日志。
    """
    try:
        collection = get_or_create_collection(get_vector_store(), _CORPUS_COLLECTION)
    except Exception as e:  # noqa: BLE001
        logger.warning("获取向量集合失败，跳过向量删除（item=%s）: %s", item_id, e)
        return
    try:
        collection.delete(where={"source_item_id": item_id})
    except Exception as e:  # noqa: BLE001
        logger.warning("按 source_item_id 删向量失败（item=%s）: %s", item_id, e)
    try:
        collection.delete(ids=[item_id])  # 旧数据兜底
    except Exception as e:  # noqa: BLE001
        logger.warning("按 id 删向量失败（item=%s）: %s", item_id, e)


def delete_corpus(
    item_id: str,
    db: Session,
    user_id: Optional[str] = None,
    is_admin: bool = False,
) -> bool:
    query = db.query(CorpusItem).filter(CorpusItem.id == item_id)
    if not is_admin:
        query = query.filter(CorpusItem.user_id == user_id)
    item = query.first()
    if not item:
        return False
    db.delete(item)
    db.commit()
    _delete_embeddings(item_id)
    return True


def delete_corpus_by_source(
    source_id: str,
    item_type: Optional[str] = None,
    db: Optional[Session] = None,
) -> int:
    """删除某条业务数据（简历/JD/面经）贡献的全部语料副本，返回删除条数。

    级联清理用：删除业务数据时调用，清掉它贡献到共享语料库的副本。
    无 db（或不存在匹配）时返回 0。
    """
    if not source_id or db is None:
        return 0
    q = db.query(CorpusItem).filter(CorpusItem.source_id == source_id)
    if item_type:
        q = q.filter(CorpusItem.item_type == item_type)
    items = q.all()
    if not items:
        return 0
    for item in items:
        db.delete(item)
        _delete_embeddings(item.id)
    db.commit()
    return len(items)


def count_corpus(db: Session) -> Dict[str, int]:
    from sqlalchemy import func
    rows = db.query(CorpusItem.item_type, func.count(CorpusItem.id)).group_by(CorpusItem.item_type).all()
    return {item_type: int(n) for item_type, n in rows}


# ============ 贡献开关（per-user 同意） ============

def set_corpus_consent(db: Session, user_id: str, allow: bool) -> bool:
    from app.models.user import User
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    user.allow_corpus = bool(allow)
    db.commit()
    return True


def get_corpus_consent(db: Session, user_id: str) -> bool:
    from app.models.user import User
    user = db.query(User).filter(User.id == user_id).first()
    return bool(user and user.allow_corpus)
