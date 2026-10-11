"""本地向量索引：单文件 SQLite（元数据 + 向量 BLOB）+ numpy 余弦检索。

为什么不用 ChromaDB：
    实测本机语料规模约 362 条、切块后约 400+ 个 512 维向量，
    而 chromadb 会拖入 76 MB / 61 个包（含 opentelemetry、kubernetes、
    uvicorn 等服务端栈，还带遥测上报），属于严重过度设计。
    400 个 512 维向量的暴力余弦检索是 **亚毫秒级**（numpy 一次矩阵乘），
    根本不需要 ANN 索引；用标准库 sqlite3 + 已有的 numpy 即可，
    对安装包体积 0 增量，且完全离线、无遥测。

存储布局（`local_ai/index.db`）：
    chunks 表
        id          TEXT PRIMARY KEY   -- 稳定主键：{source}:{source_id}[#chunkN]
        source      TEXT               -- corpus / resume / jd
        source_id   TEXT               -- 原始条目 id（与服务器一致，便于对齐）
        item_type   TEXT               -- resume / jd / interview（用于过滤）
        chunk_index INT                -- 该条目内第几块（0 起）
        chunk_total INT                -- 该条目共几块
        text        TEXT               -- 块原文（检索命中后给 LLM 做上下文）
        vec         BLOB               -- float32 小端，**已 L2 归一化** → 点积即余弦
        dim         INT
        created_at  TEXT

    向量预先归一化存储，检索时只需 `V @ q`，省一次逐行除法。

可替换性：
    所有读写都收在 `VectorStore` 里。若将来真要换 ANN（hnswlib/chroma），
    只需另写一个同接口的类，上层 index.py / retrieve 不必改。

多表复用（2026-09-30，P3c）：
    `table` 参数让同一个类同时服务两个库表 —— 向量索引用默认的 `chunks`
    （`index.db`），**记忆**用 `memories`（`knowledge.db`）。两者必须分库：
    记忆若混进 `chunks`，`rebuild()` 的 `store.clear()` 会把用户记忆一起清空。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional

import numpy as np


def _schema(table: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    item_type   TEXT,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_total INTEGER NOT NULL DEFAULT 1,
    text        TEXT NOT NULL,
    vec         BLOB NOT NULL,
    dim         INTEGER NOT NULL,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_{table}_source    ON {table}(source);
CREATE INDEX IF NOT EXISTS idx_{table}_item_type ON {table}(item_type);
CREATE INDEX IF NOT EXISTS idx_{table}_source_id ON {table}(source_id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize(vec: Iterable[float]) -> np.ndarray:
    a = np.asarray(list(vec), dtype=np.float32)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else a


class VectorStore:
    """本地轻量向量库。线程安全（内部串行化写）。

    `table` 默认 `chunks`（向量索引）。记忆层传 `memories` 并指向另一个 db 文件，
    两边互不干扰 —— 见模块头「多表复用」。
    """

    def __init__(self, db_path: str, table: str = "chunks"):
        if not _TABLE_RE.match(table or ""):
            raise ValueError(f"非法表名：{table!r}")
        self.table = table
        self.db_path = db_path
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None  # 内存缓存：{ids, vecs, rows}
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._lock:
            con = self._connect()
            try:
                con.executescript(_schema(table))
                con.commit()
            finally:
                con.close()

    # ---------- 基础 ----------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _invalidate(self) -> None:
        self._cache = None

    def exists(self) -> bool:
        if not os.path.exists(self.db_path):
            return False
        try:
            with self._lock:
                con = self._connect()
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
                    return n > 0
                finally:
                    con.close()
        except Exception:  # noqa: BLE001
            return False

    # ---------- 写入 ----------

    def clear(self) -> None:
        """清空本表（重建前调用）。只清自己这张表，不碰同库的其它表。"""
        with self._lock:
            con = self._connect()
            try:
                con.execute(f"DELETE FROM {self.table}")
                con.commit()
            finally:
                con.close()
            self._invalidate()

    def upsert_chunks(self, records: List[Dict[str, Any]], batch: int = 200) -> int:
        """批量写入块记录（向量会自动归一化）。返回写入条数。

        record 字段：id / source / source_id / item_type / chunk_index /
                     chunk_total / text / vec / created_at
        """
        if not records:
            return 0
        rows = []
        for r in records:
            v = _normalize(r["vec"])
            rows.append((
                r["id"], r["source"], r["source_id"], r.get("item_type"),
                int(r.get("chunk_index", 0)), int(r.get("chunk_total", 1)),
                r.get("text", ""), v.tobytes(), int(v.shape[0]),
                r.get("created_at"),
            ))
        with self._lock:
            con = self._connect()
            try:
                for i in range(0, len(rows), batch):
                    con.executemany(
                        f"INSERT OR REPLACE INTO {self.table} "
                        "(id, source, source_id, item_type, chunk_index, chunk_total,"
                        " text, vec, dim, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        rows[i:i + batch],
                    )
                con.commit()
            finally:
                con.close()
            self._invalidate()
        return len(rows)

    def delete_chunks(self, source: str, source_id: str) -> int:
        """删掉某条目的**全部**块（增量更新 / 删除时用）。返回删除块数。

        为什么按 (source, source_id) 而不是按 id 前缀删：
            `_chunk_id` 是 `{source}:{id}[#chunkN]`，按前缀 `LIKE` 删时要处理转义；
            直接按两个结构化列删更稳，且走 idx_chunks_source_id 索引。
        **必须先删后插**：文本改短后块数变少，只 upsert 新块会留下旧的高位块
        （如旧的 `#chunk2`），那些块内容是过期文本、还会被检索命中。
        """
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    f"DELETE FROM {self.table} WHERE source=? AND source_id=?",
                    (source, str(source_id)),
                )
                n = int(cur.rowcount or 0)
                con.commit()
            finally:
                con.close()
            self._invalidate()
        return n

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
                con.commit()
            finally:
                con.close()

    def get_meta(self, key: str) -> Optional[str]:
        try:
            with self._lock:
                con = self._connect()
                try:
                    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                    return row[0] if row else None
                finally:
                    con.close()
        except Exception:  # noqa: BLE001
            return None

    # ---------- 读取 ----------

    def _load(self) -> Dict[str, Any]:
        """把全部块读进内存（首次调用后缓存）。400 条规模下开销可忽略。"""
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is not None:
                return self._cache
            con = self._connect()
            try:
                cur = con.execute(
                    f"SELECT id, source, source_id, item_type, chunk_index, chunk_total,"
                    f" text, vec, dim FROM {self.table}"
                )
                rows = cur.fetchall()
            finally:
                con.close()
        if not rows:
            self._cache = {"rows": [], "ids": [], "vecs": np.zeros((0, 0), dtype=np.float32)}
            return self._cache
        dim = rows[0][8]
        vecs = np.zeros((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            vecs[i] = np.frombuffer(r[7], dtype=np.float32)
        self._cache = {
            "rows": rows,
            "ids": [r[0] for r in rows],
            "vecs": vecs,
        }
        return self._cache

    def all_rows(self, limit: int = 500) -> List[Dict[str, Any]]:
        """按写入时间倒序取记录（**不含向量**）——界面列表 / 去重比对用。

        记忆层要用它做「这条事实是不是已经有了」的判断，所以必须能整表扫。
        """
        t = self.table
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    f"SELECT id, source, source_id, item_type, text, created_at "
                    f"FROM {t} ORDER BY created_at DESC LIMIT ?", (int(limit),)
                )
                rows = cur.fetchall()
            finally:
                con.close()
        return [{"id": r[0], "source": r[1], "source_id": r[2],
                 "item_type": r[3], "text": r[4], "created_at": r[5]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        """索引概况：总块数、按来源/类型分布、覆盖条目数、维度。"""
        t = self.table
        try:
            with self._lock:
                con = self._connect()
                try:
                    total = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    by_source = dict(con.execute(
                        f"SELECT source, COUNT(*) FROM {t} GROUP BY source"
                    ).fetchall())
                    by_type = dict(con.execute(
                        f"SELECT COALESCE(item_type,''), COUNT(*) FROM {t} "
                        f"GROUP BY item_type"
                    ).fetchall())
                    items = con.execute(
                        f"SELECT COUNT(DISTINCT source || ':' || source_id) FROM {t}"
                    ).fetchone()[0]
                finally:
                    con.close()
            return {
                "exists": total > 0,
                "table": self.table,
                "chunks": total,
                "items": items,
                "by_source": by_source,
                "by_item_type": by_type,
                "built_at": self.get_meta("built_at"),
                # backend / model 一并暴露：UI 要显示「当前是语义检索还是降级词袋」，
                # 种子铺设也要据此校验（模型不同则向量空间不同，种子不可用）。
                "backend": self.get_meta("backend"),
                "model": self.get_meta("model"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"exists": False, "error": f"{type(exc).__name__}: {exc}"}

    def search(
        self,
        query_vec: Iterable[float],
        top_k: int = 5,
        item_type: Optional[str] = None,
        source: Optional[Any] = None,
        exclude_source: Optional[str] = None,
        min_score: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """余弦检索。返回 [{id, source, source_id, item_type, text, score, ...}]。

        `source` 支持**单值或列表**（白名单）。列表形态服务于「复用闭环」：
        生成/优化时同时取 corpus 范文与用户自己的成果池
        （`["corpus", "generated", "optimized"]`），而材料池不在白名单里，
        自引用防护由「不在名单」自然成立。

        exclude_source：**排除**某来源（与 `source` 二选一即可）。生成简历时
        传 "resume" —— 否则「我的简历」会被自己的 query 检索回来
        （实测 score=1.000），把 top_k 的第 1 位占掉，外部参考额度直接减半。

        同一条目的多个块命中时只保留最高分（与服务器 retrieve_similar 的聚合一致）。
        """
        data = self._load()
        rows = data["rows"]
        if not rows:
            return []
        q = _normalize(query_vec)
        vecs = data["vecs"]
        if vecs.shape[1] != q.shape[0]:
            raise ValueError(f"向量维度不匹配：索引 {vecs.shape[1]} vs 查询 {q.shape[0]}")
        scores = vecs @ q  # 双方都已归一化 → 点积即余弦

        # 过滤后的候选下标
        idx = range(len(rows))
        if item_type or source or exclude_source:
            want: Optional[set] = None
            if source:
                want = {source} if isinstance(source, str) else set(source)
            idx = [
                i for i in idx
                if (not item_type or (rows[i][3] or "") == item_type)
                and (want is None or rows[i][1] in want)
                and (not exclude_source or rows[i][1] != exclude_source)
            ]
        if not idx:
            return []

        order = sorted(idx, key=lambda i: -float(scores[i]))
        out: List[Dict[str, Any]] = []
        seen: Dict[str, int] = {}
        for i in order:
            if float(scores[i]) < min_score:
                break
            r = rows[i]
            key = f"{r[1]}:{r[2]}"
            if key in seen:
                continue  # 同条目只留最高分块
            seen[key] = i
            out.append({
                "id": r[0],
                "source": r[1],
                "source_id": r[2],
                "item_type": r[3],
                "chunk_index": r[4],
                "chunk_total": r[5],
                "text": r[6],
                "score": round(float(scores[i]), 4),
            })
            if len(out) >= max(1, top_k):
                break
        return out


__all__ = ["VectorStore"]
