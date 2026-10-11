"""本地只读副本：云端数据的镜像缓存。

## 为什么需要它

桌面版是**纯客户端**——所有业务数据只存在服务器 PostgreSQL 里，本地一个字节都不留。
一旦断网、服务器故障或网络抖动，`api_client` 的读接口会返回空列表，
界面显示「暂无数据」，用户会**误以为自己的简历/投递记录全都丢了**。
（实测：真·无数据、断网、登录失效三种情况返回的都是同一个 `[]`，界面无法分辨。）

本模块保存一份云端数据的本地副本，让断网时仍能**查看**自己的数据。

## 设计取舍

1. **存归一后的原始 JSON 快照，不做逐字段映射。**
   复用 `database.py` 的 11 张业务表看似省事，但云端与本地字段必然会漂移
   （加字段、改名、嵌套结构变化），映射一旦漏字段就静默丢数据。
   整份 JSON 存取则对字段变化完全免疫。
   代价是不能用 SQL 查询副本 —— 但我们只需要「整份取回」，不需要查询。

2. **按云账号隔离。** 同一台机器换账号登录不会串数据（用 account 做联合主键）。

3. **只读。** 不做离线写队列：后端不接受客户端指定 id，本地写需要额外的 id 映射表
   与冲突合并策略，属于另一个量级的工作。这里只保证「看得到」，写操作一律明确报错。

## 数据存放

`DATA_DIR/cloud_cache.db`（已安装版是 `%LOCALAPPDATA%/OfferFlow/data/cloud_cache.db`）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

_DB_PATH = os.path.join(DATA_DIR, "cloud_cache.db")

_lock = threading.RLock()
_account: Optional[str] = None
_last_error: str = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_cache (
    account    TEXT NOT NULL DEFAULT '',
    entity     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (account, entity)
);
CREATE TABLE IF NOT EXISTS cloud_cache_meta (
    account      TEXT PRIMARY KEY,
    last_sync_at TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def set_account(account: Optional[str]) -> None:
    """切换当前镜像账号（登录成功 / 登出时调用）。

    账号名取云端返回的 email（作主键用，稳定且不重名）。
    """
    global _account
    with _lock:
        _account = (account or "").strip() or None


def get_account() -> Optional[str]:
    return _account


def _acct() -> str:
    return _account or ""


def last_error() -> str:
    """最近一次镜像写入失败的原因（供诊断；镜像失败不影响主流程）。"""
    return _last_error


def mirror(entity: str, payload: Any) -> bool:
    """把一次成功的读结果写入本地副本。

    未登录时不写（没有「我的数据」概念，避免匿名数据污染）。
    失败只记录原因并返回 False —— 它是增强能力，绝不能因此让正常请求失败。
    """
    global _last_error
    acct = _acct()
    if not acct:
        return False
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        _last_error = f"序列化失败：{exc}"
        return False

    count = len(payload) if isinstance(payload, (list, dict)) else 1
    try:
        with _lock:
            conn = _conn()
            try:
                now = _now()
                conn.execute(
                    "INSERT INTO cloud_cache(account, entity, payload, item_count, synced_at) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(account, entity) DO UPDATE SET payload=excluded.payload, "
                    "item_count=excluded.item_count, synced_at=excluded.synced_at",
                    (acct, entity, blob, count, now),
                )
                conn.execute(
                    "INSERT INTO cloud_cache_meta(account, last_sync_at) VALUES(?,?) "
                    "ON CONFLICT(account) DO UPDATE SET last_sync_at=excluded.last_sync_at",
                    (acct, now),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except sqlite3.Error as exc:
        _last_error = f"写入失败：{exc}"
        return False


def read(entity: str) -> Any:
    """读本地副本；不存在或解析失败返回 None（调用方自行兜底成空列表）。"""
    acct = _acct()
    if not acct:
        return None
    try:
        with _lock:
            conn = _conn()
            try:
                row = conn.execute(
                    "SELECT payload FROM cloud_cache WHERE account=? AND entity=?",
                    (acct, entity),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return json.loads(row["payload"])
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def synced_at(entity: Optional[str] = None) -> Optional[str]:
    """副本的同步时间：给定 entity 取该项，否则取该账号最近一次同步。"""
    acct = _acct()
    if not acct:
        return None
    try:
        with _lock:
            conn = _conn()
            try:
                if entity:
                    row = conn.execute(
                        "SELECT synced_at FROM cloud_cache WHERE account=? AND entity=?",
                        (acct, entity),
                    ).fetchone()
                    return row["synced_at"] if row else None
                row = conn.execute(
                    "SELECT last_sync_at FROM cloud_cache_meta WHERE account=?", (acct,)
                ).fetchone()
                return row["last_sync_at"] if row else None
            finally:
                conn.close()
    except sqlite3.Error:
        return None


def entities() -> List[Dict[str, Any]]:
    """当前账号已有副本的实体清单（含条数与时间），供设置页展示。"""
    acct = _acct()
    if not acct:
        return []
    try:
        with _lock:
            conn = _conn()
            try:
                rows = conn.execute(
                    "SELECT entity, item_count, synced_at FROM cloud_cache "
                    "WHERE account=? ORDER BY entity",
                    (acct,),
                ).fetchall()
            finally:
                conn.close()
        return [{"entity": r["entity"], "count": r["item_count"], "synced_at": r["synced_at"]}
                for r in rows]
    except sqlite3.Error:
        return []


def has_any() -> bool:
    """本地是否有任何可用的副本数据（界面据此决定是否提示「可离线查看」）。"""
    return bool(entities())


def stats() -> Dict[str, Any]:
    """概览：账号、实体数、总条数、最近同步时间、库文件大小。"""
    ents = entities()
    size = 0
    try:
        if os.path.exists(_DB_PATH):
            size = os.path.getsize(_DB_PATH)
    except OSError:
        pass
    return {
        "account": _account,
        "entity_count": len(ents),
        "item_total": sum(e["count"] for e in ents),
        "last_sync_at": synced_at(),
        "db_path": _DB_PATH,
        "db_size": size,
        "entities": ents,
        "last_error": _last_error,
    }


def clear(account: Optional[str] = None) -> int:
    """清除副本（默认清当前账号；传 "*" 清全部）。返回删除行数。"""
    acct = _acct() if account is None else account
    try:
        with _lock:
            conn = _conn()
            try:
                if acct == "*":
                    cur = conn.execute("DELETE FROM cloud_cache")
                    conn.execute("DELETE FROM cloud_cache_meta")
                else:
                    cur = conn.execute("DELETE FROM cloud_cache WHERE account=?", (acct,))
                    conn.execute("DELETE FROM cloud_cache_meta WHERE account=?", (acct,))
                n = cur.rowcount
                conn.commit()
            finally:
                conn.close()
        return n
    except sqlite3.Error:
        return 0
