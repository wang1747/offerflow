"""长期记忆仓储：Postgres 记录 + Chroma 向量索引。

## 事实源在哪（设计文档 15.2 的硬要求）

**Postgres 是事实源**；Chroma 只是"帮我找到 memory_id"的索引。
召回流程因此是两段：先向量召回拿到 ``memory_id``，再**回表按 user_id + status
重新校验**并取内容。绝不把向量相似度直接当成事实——
15.2 节原话："Chroma 结果必须带 ``memory_id``，不能只靠向量相似度当作事实"。

## proposed 不进索引（结构性地杜绝"模型自授权"）

只有 ``confirmed`` 的记忆会被写进 Chroma。这意味着「模型推断的偏好（proposed）
被召回并被当成事实」这件事**在结构上不可能发生**——它压根不在索引里，
不是靠"召回了再过滤"这种一步就能写漏的写法。

## 为什么独立集合

设计文档 15 节要求长期记忆的语义索引与业务向量分开。这里固定用
``agent_memory`` 集合，**不得复用 ``jd_embeddings``**：两者混在一起会让
"删记忆要同步删哪些向量"变成一次全表扫描式的猜测，也把用户记忆
混进了 JD 语料的空间。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.models.agent_memory import AgentMemory, MEMORY_SOURCES, MEMORY_TYPES

logger = logging.getLogger(__name__)

#: 长期记忆的独立向量集合（**不得**改成 jd_embeddings）
AGENT_MEMORY_COLLECTION = "agent_memory"

#: 单条记忆的字符上限。记忆是"短判断"，不是正文搬运通道。
#: 这个上限同时也是 15.3 节「禁止完整简历/JD 正文入库」的结构性防线：
#: 完整正文不可能短于 500 字。
MAX_MEMORY_CONTENT_CHARS = 500
#: 行数上限。简历/JD 正文都是多行长文，记忆应当是 1~3 行短句。
MAX_MEMORY_LINES = 3

#: 凭据特征。命中即拒绝写入（15.3 节明令禁止）。
_CREDENTIAL_MARKERS = (
    "-----begin", "private key", "sk-", "bearer ", "api_key", "apikey",
    "password:", "passwd", "authorization:", "cookie:", "token:",
)

_collection_cache: Optional[Any] = None


class MemoryContentRejected(ValueError):
    """记忆内容违反 15.3 节的禁止清单。"""


def assert_memory_content_allowed(content: str) -> None:
    """写入前的结构约束。**在仓储层拦，不靠事后审计。**

    判据都是"结构性"的（长度、行数、凭据特征串），而不是试图判断
    "这段文字是不是简历正文"——后者不可能可靠，前者是确定的。
    """
    text = content or ""
    if not text.strip():
        raise MemoryContentRejected("记忆内容不能为空")
    if len(text) > MAX_MEMORY_CONTENT_CHARS:
        raise MemoryContentRejected(
            f"记忆内容不得超过 {MAX_MEMORY_CONTENT_CHARS} 字（实际 {len(text)}）——"
            "长期记忆只存短判断，不存完整简历/JD 正文"
        )
    line_count = text.count("\n") + 1
    if line_count > MAX_MEMORY_LINES:
        raise MemoryContentRejected(
            f"记忆内容不得多于 {MAX_MEMORY_LINES} 行（实际 {line_count} 行）——"
            "多行长文疑似正文搬运"
        )
    lowered = text.lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            raise MemoryContentRejected(
                f"记忆内容疑似包含凭据信息（命中特征 {marker!r}），禁止写入"
            )


def normalize_content(content: str) -> str:
    """规范化文本：折叠空白、统一小写，供向量化使用。

    与 ``content`` 分开存（后者给人看、保留原始大小写与标点）。
    """
    return " ".join((content or "").split()).lower()


def _embed(text: str) -> List[float]:
    """本地 embedding；模型不可用时降级哈希词袋（与 match 模块同一策略）。"""
    from app.core.embeddings import embed_hash_fallback, embed_text

    vec = embed_text(text)
    if vec is None:
        vec = embed_hash_fallback(text)
    return [float(x) for x in vec]


def get_memory_collection():
    """取（并缓存）``agent_memory`` 集合。

    集合用 cosine 距离——召回时 ``score = 1 - distance`` 才是可解释的相似度。
    """
    global _collection_cache
    if _collection_cache is None:
        from app.core.vector_store import get_or_create_collection, get_vector_store

        client = get_vector_store()
        _collection_cache = get_or_create_collection(
            client, name=AGENT_MEMORY_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection_cache


def _reset_collection_cache_for_tests() -> None:
    """仅测试：让 Chroma 集合句柄随测试目录一起重建。"""
    global _collection_cache
    _collection_cache = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryRepository:
    """按用户隔离的记忆读写。``user_id`` 由运行时注入，**不来自模型或请求体**。"""

    def __init__(self, db, user_id: str) -> None:
        self._db = db
        self._user_id = user_id

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        content: str,
        memory_type: str,
        source: str,
        status: str,
        source_refs: Optional[Sequence[str]] = None,
        confidence: float = 0.0,
        importance: float = 0.5,
        conflict_key: Optional[str] = None,
        ttl_days: Optional[int] = None,
    ) -> AgentMemory:
        """写入一条记忆。

        - ``status="proposed"``：只落 Postgres，**不进向量索引**（不会被召回）。
        - ``status="confirmed"``：落 Postgres 并写向量。

        ``conflict_key`` 非空且已存在同键的有效记忆时，旧记录被标 ``superseded``
        并从索引中撤下——"保留新记录、不静默覆盖"（15.2 节）。
        """
        assert_memory_content_allowed(content)
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"非法记忆类型：{memory_type}")
        if source not in MEMORY_SOURCES:
            raise ValueError(f"非法记忆来源：{source}")
        if status not in ("proposed", "confirmed"):
            raise ValueError(f"新建记忆的状态只能是 proposed / confirmed：{status}")

        superseded_ids: List[str] = []
        if conflict_key:
            superseded_ids = self._supersede_by_key(conflict_key)

        memory = AgentMemory(
            memory_id=uuid.uuid4().hex,
            user_id=self._user_id,
            memory_type=memory_type,
            status=status,
            content=content,
            normalized_content=normalize_content(content),
            source=source,
            conflict_key=conflict_key,
            source_refs=list(source_refs or []),
            confidence=float(confidence),
            importance=float(importance),
            expires_at=(_now() + timedelta(days=ttl_days)) if ttl_days else None,
        )
        self._db.add(memory)
        self._db.commit()
        self._db.refresh(memory)

        # 让旧记忆指向新记录（"曾被什么取代"必须可追溯）
        for old_id in superseded_ids:
            self._point_superseded_to(old_id, memory.memory_id)

        if memory.status == "confirmed":
            self._upsert_vector(memory)
        return memory

    def confirm(self, memory_id: str) -> Optional[AgentMemory]:
        """用户确认：``proposed`` → ``confirmed``，并开始进向量索引。

        **只有用户能走这条路径**（API 层校验），模型/Graph 不能自行调用。
        """
        memory = self.get(memory_id)
        if memory is None:
            return None
        if memory.status == "confirmed":
            return memory          # 幂等：重复确认不改变事实
        if memory.status not in ("proposed",):
            raise ValueError(f"该记忆当前状态不可确认：{memory.status}")

        memory.status = "confirmed"
        memory.source = "user_confirmed"
        memory.updated_at = _now()
        self._db.commit()
        self._db.refresh(memory)
        self._upsert_vector(memory)
        return memory

    def delete(self, memory_id: str) -> bool:
        """删除记忆：**同步删除结构化记录与向量索引**（8.2 / 15.2 节）。"""
        memory = self.get(memory_id)
        if memory is None:
            return False
        self._db.delete(memory)
        self._db.commit()
        self._delete_vector(memory_id)
        return True

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get(self, memory_id: str) -> Optional[AgentMemory]:
        """取单条。**按 user_id 过滤**——他人记忆与不存在返回同一个 None。"""
        return (
            self._db.query(AgentMemory)
            .filter(
                AgentMemory.memory_id == memory_id,
                AgentMemory.user_id == self._user_id,
            )
            .first()
        )

    def list(
        self,
        *,
        status: Optional[str] = None,
        memory_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[AgentMemory]:
        """列出本用户的记忆（默认不含 superseded——它们已不在召回面）。"""
        query = self._db.query(AgentMemory).filter(
            AgentMemory.user_id == self._user_id,
        )
        if status:
            query = query.filter(AgentMemory.status == status)
        else:
            query = query.filter(AgentMemory.status != "superseded")
        if memory_type:
            query = query.filter(AgentMemory.memory_type == memory_type)
        return query.order_by(AgentMemory.created_at.desc()).limit(limit).all()

    def recall(self, query_text: str, *, k: int = 5) -> List[Dict[str, Any]]:
        """语义召回**已确认**的记忆。

        返回 ``[{memory_id, content, score, memory_type}]``。索引里只有 confirmed，
        但回表时仍**再核一次** user_id + status：索引可能滞后（删除失败、
        并发写入），而事实必须来自 Postgres。
        """
        if not (query_text or "").strip():
            return []

        try:
            collection = get_memory_collection()
            total = collection.count()
            if total == 0:
                return []
            res = collection.query(
                query_embeddings=[_embed(query_text)],
                n_results=max(1, min(k, total)),
                where={"user_id": self._user_id},
                include=["distances"],
            )
        except Exception:  # noqa: BLE001
            # 向量层不可用**不能**让整轮 Agent 失败——记忆是增量能力，
            # 降级为空召回，并且调用方看到的 recalled 就是空列表（不伪装）。
            logger.warning("记忆召回失败，降级为空召回", exc_info=True)
            return []

        ids = (res.get("ids") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        out: List[Dict[str, Any]] = []
        for idx, memory_id in enumerate(ids):
            memory = self.get(memory_id)          # 回表校验（事实源）
            if memory is None or memory.status != "confirmed":
                continue
            distance = float(distances[idx]) if idx < len(distances) else 1.0
            out.append({
                "memory_id": memory.memory_id,
                "content": memory.content,
                "memory_type": memory.memory_type,
                "score": round(max(0.0, 1.0 - distance), 4),
            })
        return out

    def pool_fingerprint(self) -> str:
        """本用户有效记忆集合的指纹（供"记忆确实参与了本轮"这类断言使用）。"""
        rows = self.list(limit=1000)
        digest = hashlib.sha256()
        for row in sorted(rows, key=lambda r: r.memory_id):
            digest.update(f"{row.memory_id}:{row.status}:{row.content}".encode("utf-8"))
        return digest.hexdigest()[:16]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _supersede_by_key(self, conflict_key: str) -> List[str]:
        """把同冲突键的现存有效记忆标 superseded，返回被取代的 id 列表。"""
        rows = (
            self._db.query(AgentMemory)
            .filter(
                AgentMemory.user_id == self._user_id,
                AgentMemory.conflict_key == conflict_key,
                AgentMemory.status.in_(("proposed", "confirmed")),
            )
            .all()
        )
        superseded: List[str] = []
        for row in rows:
            row.status = "superseded"
            row.updated_at = _now()
            superseded.append(row.memory_id)
            self._delete_vector(row.memory_id)
        if superseded:
            self._db.commit()
        return superseded

    def _point_superseded_to(self, old_id: str, new_id: str) -> None:
        old = (
            self._db.query(AgentMemory)
            .filter(
                AgentMemory.memory_id == old_id,
                AgentMemory.user_id == self._user_id,
            )
            .first()
        )
        if old is None:
            return
        old.superseded_by = new_id
        self._db.commit()

    def _upsert_vector(self, memory: AgentMemory) -> None:
        """把一条已确认记忆写进向量索引（metadata 带 user_id，供跨用户过滤）。"""
        try:
            get_memory_collection().upsert(
                ids=[memory.memory_id],
                embeddings=[_embed(memory.normalized_content or memory.content)],
                documents=[memory.content],
                metadatas=[{
                    "user_id": memory.user_id,
                    "memory_type": memory.memory_type,
                    "status": memory.status,
                }],
            )
        except Exception:  # noqa: BLE001
            # 索引写失败不回滚 Postgres：事实源已经是干净的，索引可重建。
            # 但必须留日志——静默失败会让"确认了却召回不到"变成谜。
            logger.warning(
                "记忆向量写入失败：memory_id=%s（Postgres 已写入，索引待重建）",
                memory.memory_id, exc_info=True,
            )

    def _delete_vector(self, memory_id: str) -> None:
        try:
            get_memory_collection().delete(ids=[memory_id])
        except Exception:  # noqa: BLE001
            logger.warning("记忆向量删除失败：memory_id=%s", memory_id, exc_info=True)


def memory_payload_for_model(repo: MemoryRepository, query: str, *, k: int = 5) -> Dict[str, Any]:
    """给模型看的记忆视图（只有已确认记忆的短文本 + id）。"""
    recalled = repo.recall(query, k=k)
    return {
        "recalled": recalled,
        "note": "recalled 是用户已确认的长期偏好/决策，可作为规划依据；不得改写或删除。",
    }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
