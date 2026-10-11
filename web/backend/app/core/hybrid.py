# -*- coding: utf-8 -*-
"""混合检索：BM25 关键词检索 + RRF 融合（与向量检索组合，零外部依赖）。

设计动机：
  - 语料是「技术术语密集」型（Java/Spring/PyTorch/RAG/FastAPI 等精确词），
    纯向量对精确术语容易漂移；BM25 关键词匹配对这类词天然友好。
  - 自实现 Okapi BM25，避免引入 rank_bm25/jieba 依赖（免重建镜像）。
  - 中文用「字符 bigram」分词（无词典、轻量、对短文本效果尚可），
    英文/数字按词切分（技术术语完整保留，且与 embedding 侧一致先 lower）。

融合用 RRF（Reciprocal Rank Fusion）：score(doc) = Σ 1/(k + rank_i)，k=60。
RRF 不依赖两路分数的量纲，工程上最稳。
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.corpus import CorpusItem

_K1 = 1.5
_B = 0.75
_RRF_K = 60

# BM25 索引缓存：语料只增不减，用 (总条数, 最新创建时间) 做签名，变了才重建。
_bm25_cache: Dict[str, object] = {"sig": None, "index": None, "meta": {}}


def tokenize(text: str) -> List[str]:
    """英文/数字按词、中文按字符 bigram，统一 lower。"""
    t = (text or "").lower()
    tokens: List[str] = re.findall(r"[a-z0-9]+", t)
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


class BM25Index:
    """经典 Okapi BM25（内存索引，面向几千条以内语料）。"""

    def __init__(self, docs: List[Tuple[str, str]]):
        self.docs = docs                       # [(id, raw_text)]
        self.ids = [d[0] for d in docs]
        self.tokenized = [tokenize(d[1]) for d in docs]
        self.doc_len = [len(t) for t in self.tokenized]
        self.n = len(docs)
        self.avgdl = sum(self.doc_len) / max(1, self.n)
        self.df: Dict[str, int] = {}
        for tk in self.tokenized:
            for term in set(tk):
                self.df[term] = self.df.get(term, 0) + 1

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query: str, top_k: int) -> List[Tuple[str, float]]:
        """返回 [(id, bm25_score)]，按分数降序。"""
        q = tokenize(query)
        if not q:
            return []
        scored: List[Tuple[int, float]] = []
        for i, tk in enumerate(self.tokenized):
            tf: Dict[str, int] = {}
            for term in tk:
                tf[term] = tf.get(term, 0) + 1
            s = 0.0
            for term in set(q):
                if term not in tf:
                    continue
                f = tf[term]
                denom = f + _K1 * (1 - _B + _B * self.doc_len[i] / max(1.0, self.avgdl))
                s += self._idf(term) * (f * (_K1 + 1)) / denom
            if s > 0:
                scored.append((i, s))
        scored.sort(key=lambda x: -x[1])
        return [(self.ids[i], s) for i, s in scored[:top_k]]


def _get_bm25(db: Session):
    """带缓存地构建全量语料的 BM25 索引；签名变化（语料增减）时自动重建。"""
    sig_row = db.query(func.count(CorpusItem.id), func.max(CorpusItem.created_at)).one()
    sig = (int(sig_row[0] or 0), str(sig_row[1] or ""))
    if _bm25_cache["sig"] == sig:
        return _bm25_cache["index"], _bm25_cache["meta"]

    rows = db.query(CorpusItem.id, CorpusItem.raw_text,
                    CorpusItem.item_type, CorpusItem.direction).all()
    index = BM25Index([(r.id, r.raw_text or "") for r in rows])
    meta = {r.id: (r.item_type, r.direction) for r in rows}
    _bm25_cache["sig"] = sig
    _bm25_cache["index"] = index
    _bm25_cache["meta"] = meta
    return index, meta


def bm25_search(db: Session, query: str, top_k: int,
                item_type: Optional[str] = None,
                direction: Optional[str] = None) -> List[Tuple[str, float]]:
    """BM25 检索 + item_type/direction 过滤，返回 [(id, score)]。"""
    index, meta = _get_bm25(db)
    out: List[Tuple[str, float]] = []
    # 多召回一些，过滤后再截断，避免过滤后不足 top_k
    for cid, score in index.search(query, top_k * 5):
        it, dr = meta.get(cid, (None, None))
        if item_type and it != item_type:
            continue
        if direction and dr != direction:
            continue
        out.append((cid, score))
        if len(out) >= top_k:
            break
    return out


def rrf_fuse(vector_hits: List[Tuple[str, float]],
             bm25_hits: List[Tuple[str, float]],
             k: int = _RRF_K) -> List[Tuple[str, float]]:
    """RRF 融合两路召回，返回 [(id, rrf_score)] 降序。"""
    scores: Dict[str, float] = {}
    for rank, (cid, _s) in enumerate(vector_hits):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    for rank, (cid, _s) in enumerate(bm25_hits):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])
