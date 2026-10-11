"""本地业务主存 + 待上行队列（P4）。

## 与 local_cache.py 的分工

    local_cache —— 服务端返回的**只读快照**（用于「数据截至」提示与断网可看）
    local_store —— **本地可写主存**：离线也能新建/修改/删除，改动进 outbox 排队，
                   有网时按 30 分钟周期上行（见 services/sync.py）

## 为什么需要它

此前桌面版断网时只能「看」，任何写操作都直接失败（`api_client` 明确抛错）——
用户在地铁上记一条投递，会被拒绝且内容丢失。P4 让写**先落本地**（立即可见、不丢），
联网后自动补传，这才是「桌面原生 + 云同步」。

## 幂等靠什么

客户端自己生成 id（uuid4），上行时一并提交；服务端 create 接口已支持接收 `id`
并保证「同用户同 id 只建一条」（见后端 `application/services.create_application`
与 `resume/services.upload_resume` 的 client_id 分支）。因此重复上行**不会产生重复数据**，
不需要额外的 id 映射表。

## 状态机（biz.state）

    synced  与服务端一致
    pending 本地有未上行改动（新建未成功上行的也在此态）
    deleted 本地已删、待上行删除（**读时过滤**，避免同步失败后「删了又冒出来」）

## 表格

    biz       本地业务记录（account + entity + id 主键，payload 存整条 JSON）
    outbox    待上行操作队列（每个 (entity,id) 至多一条：新写入会合并覆盖旧的）
    sync_meta 每账号最近同步时间与结果

数据存放：`DATA_DIR/biz_local.db`（已安装版在 `%LOCALAPPDATA%/OfferFlow/data/`）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(DATA_DIR, "biz_local.db")

_lock = threading.RLock()
_account: Optional[str] = None
_last_error: str = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS biz (
    account    TEXT NOT NULL,
    entity     TEXT NOT NULL,
    id         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'synced',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account, entity, id)
);
CREATE TABLE IF NOT EXISTS outbox (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    account    TEXT NOT NULL,
    entity     TEXT NOT NULL,
    op         TEXT NOT NULL,
    id         TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_acct ON outbox(account, seq);
CREATE TABLE IF NOT EXISTS sync_meta (
    account      TEXT PRIMARY KEY,
    last_sync_at TEXT,
    last_result  TEXT
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
    """切换当前账号（登录成功 / 登出时调用）。"""
    global _account
    with _lock:
        _account = (account or "").strip() or None


def get_account() -> Optional[str]:
    return _account


def _acct() -> str:
    return _account or ""


def last_error() -> str:
    """最近一次本地写入/合并失败的原因（供诊断）。"""
    return _last_error


# ============ 本地写（新建 / 更新） ============

def save(entity: str, op: str, item_id: str, payload: Any) -> Optional[int]:
    """本地保存一条记录并入队待上行，返回 outbox 的 seq（供上行后回填）。

    op: 'create' | 'update'。payload 为完整记录 dict（既用于本地读，也作为上行体）。
    同一 (entity, id) 只保留一条待上行记录（新的覆盖旧的），避免重复上行。
    未登录或写入失败返回 None。
    """
    global _last_error
    acct, item_id = _acct(), str(item_id or "")
    if not acct or not entity or not item_id:
        return None
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        _last_error = f"序列化失败：{exc}"
        return None

    now = _now()
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT INTO biz(account, entity, id, payload, state, updated_at) "
                    "VALUES(?,?,?,?,'pending',?) "
                    "ON CONFLICT(account, entity, id) DO UPDATE SET "
                    "payload=excluded.payload, state='pending', updated_at=excluded.updated_at",
                    (acct, entity, item_id, blob, now),
                )
                # 队列合并：同一 (entity, id) 只保留一条待上行记录。
                # 两条规则（缺了会静默丢数据）：
                #   1. 队里已是 create 而新 op 是 update → **仍保持 create**。
                #      服务端还没有这条记录，只发 PUT 会打到空处（真实后端 404），
                #      记录就永远建不出来 —— 必须让 create 先发生（payload 已是最新内容）。
                #   2. 队里已是 delete → 保持 delete（删除意愿已定，不被后续改动推翻）。
                row = conn.execute(
                    "SELECT op FROM outbox WHERE account=? AND entity=? AND id=?",
                    (acct, entity, item_id),
                ).fetchone()
                old_op = row["op"] if row else None
                if old_op in ("create", "delete"):
                    final_op = old_op
                else:
                    final_op = op
                conn.execute(
                    "DELETE FROM outbox WHERE account=? AND entity=? AND id=?",
                    (acct, entity, item_id),
                )
                cur = conn.execute(
                    "INSERT INTO outbox(account, entity, op, id, payload, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (acct, entity, final_op, item_id, blob, now),
                )
                seq = int(cur.lastrowid or 0)
                conn.commit()
            finally:
                conn.close()
        return seq
    except sqlite3.Error as exc:
        _last_error = f"本地写入失败：{exc}"
        return None


def soft_delete(entity: str, item_id: str) -> Optional[int]:
    """本地删除，返回待上行 delete 的 seq（无需上行则 None）。

    若该记录尚未上行成功（outbox 里就是它的 create），则**直接撤销**：
    从 biz 真删 + 丢掉队列项（服务端从没见过它），返回 None。
    否则标记 state='deleted' 并入队 delete（读时过滤），返回其 seq。
    """
    global _last_error
    acct, item_id = _acct(), str(item_id or "")
    if not acct or not entity or not item_id:
        return None
    now = _now()
    try:
        with _lock:
            conn = _conn()
            try:
                row = conn.execute(
                    "SELECT op FROM outbox WHERE account=? AND entity=? AND id=?",
                    (acct, entity, item_id),
                ).fetchone()
                if row and row["op"] == "create":
                    conn.execute("DELETE FROM outbox WHERE account=? AND entity=? AND id=?",
                                 (acct, entity, item_id))
                    conn.execute("DELETE FROM biz WHERE account=? AND entity=? AND id=?",
                                 (acct, entity, item_id))
                    seq: Optional[int] = None
                else:
                    conn.execute(
                        "UPDATE biz SET state='deleted', updated_at=? "
                        "WHERE account=? AND entity=? AND id=?",
                        (now, acct, entity, item_id),
                    )
                    conn.execute("DELETE FROM outbox WHERE account=? AND entity=? AND id=?",
                                 (acct, entity, item_id))
                    cur = conn.execute(
                        "INSERT INTO outbox(account, entity, op, id, payload, created_at) "
                        "VALUES(?,?,'delete',?,NULL,?)",
                        (acct, entity, item_id, now),
                    )
                    seq = int(cur.lastrowid or 0)
                conn.commit()
            finally:
                conn.close()
        return seq
    except sqlite3.Error as exc:
        _last_error = f"本地删除失败：{exc}"
        return None


def restore(entity: str, item_id: str, payload: Any, state: str = "synced") -> bool:
    """回滚本地记录到指定内容，并**取消**它的待上行改动。

    用于「上行被服务端明确拒绝」（4xx）：本地不能停留在用户改过的值上（服务端没收），
    也不该继续排队重试 —— 否则每次同步都会失败，队列永远清不掉。
    """
    global _last_error
    acct, item_id = _acct(), str(item_id or "")
    if not acct or not entity or not item_id:
        return False
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        _last_error = f"序列化失败：{exc}"
        return False
    now = _now()
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute("DELETE FROM outbox WHERE account=? AND entity=? AND id=?",
                             (acct, entity, item_id))
                conn.execute(
                    "INSERT INTO biz(account, entity, id, payload, state, updated_at) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(account, entity, id) DO UPDATE SET "
                    "payload=excluded.payload, state=excluded.state, updated_at=excluded.updated_at",
                    (acct, entity, item_id, blob, state, now),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except sqlite3.Error as exc:
        _last_error = f"回滚失败：{exc}"
        return False


# ============ 本地读 ============

def list_items(entity: str) -> List[Dict[str, Any]]:
    """本地记录列表（过滤已删除），按 updated_at 倒序。

    **兜底补 id**（2026-09-30 修）：主键存在 `biz.id` 列里，而消费方
    （`index._local_records` / `local_ai.graph._own_docs`）都是读 `payload["id"]`。
    正常路径下两者一致（`api_client` 会把 id 写进 payload），但只要有一条 payload
    缺 id，那份数据就会**被静默跳过** —— 既不进向量索引、也不参与建图，而且完全
    没有报错，排查起来极难。这里用列里的 id 兜底，把这种可能性直接消灭。
    """
    acct = _acct()
    if not acct:
        return []
    try:
        with _lock:
            conn = _conn()
            try:
                rows = conn.execute(
                    "SELECT id, payload, state FROM biz "
                    "WHERE account=? AND entity=? AND state<>'deleted' "
                    "ORDER BY updated_at DESC",
                    (acct, entity),
                ).fetchall()
            finally:
                conn.close()
        out = []
        for r in rows:
            try:
                item = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                item.setdefault("id", r["id"])      # 见 docstring：缺 id 会被静默跳过
                item["_local_state"] = r["state"]   # synced / pending
                out.append(item)
        return out
    except sqlite3.Error:
        return []


def get_item(entity: str, item_id: str) -> Optional[Dict[str, Any]]:
    acct = _acct()
    if not acct:
        return None
    try:
        with _lock:
            conn = _conn()
            try:
                row = conn.execute(
                    "SELECT payload, state FROM biz "
                    "WHERE account=? AND entity=? AND id=? AND state<>'deleted'",
                    (acct, entity, str(item_id)),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        item = json.loads(row["payload"])
        if isinstance(item, dict):
            item["_local_state"] = row["state"]
        return item if isinstance(item, dict) else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


# ============ 下行（服务端 → 本地） ============

def replace_synced(entity: str, items: List[Dict[str, Any]]) -> int:
    """用服务端返回的整批记录刷新本地。

    两条保护规则：
      1. 本地 state != 'synced' 的 id **不被覆盖**（用户有未上行的改动，以本地为准）；
      2. 服务端已经没有、而本地是 synced 的 → 删掉（让服务端删除也能同步下来）。
    """
    global _last_error
    acct = _acct()
    if not acct or not isinstance(items, list):
        return 0
    now = _now()
    n = 0
    try:
        with _lock:
            conn = _conn()
            try:
                protected = {r["id"] for r in conn.execute(
                    "SELECT id FROM biz WHERE account=? AND entity=? AND state<>'synced'",
                    (acct, entity)).fetchall()}
                remote_ids = set()
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    rid = str(it.get("id") or "")
                    if not rid:
                        continue
                    remote_ids.add(rid)
                    if rid in protected:
                        continue
                    conn.execute(
                        "INSERT INTO biz(account, entity, id, payload, state, updated_at) "
                        "VALUES(?,?,?,?,'synced',?) "
                        "ON CONFLICT(account, entity, id) DO UPDATE SET "
                        "payload=excluded.payload, state='synced', updated_at=excluded.updated_at",
                        (acct, entity, rid, json.dumps(it, ensure_ascii=False), now),
                    )
                    n += 1
                stale = {r["id"] for r in conn.execute(
                    "SELECT id FROM biz WHERE account=? AND entity=? AND state='synced'",
                    (acct, entity)).fetchall()} - remote_ids
                for rid in stale:
                    conn.execute("DELETE FROM biz WHERE account=? AND entity=? AND id=?",
                                 (acct, entity, rid))
                conn.commit()
            finally:
                conn.close()
        return n
    except sqlite3.Error as exc:
        _last_error = f"下行合并失败：{exc}"
        return 0


# ============ outbox ============

def pending_ops(limit: int = 200) -> List[Dict[str, Any]]:
    """待上行操作（按入队顺序）。"""
    acct = _acct()
    if not acct:
        return []
    try:
        with _lock:
            conn = _conn()
            try:
                rows = conn.execute(
                    "SELECT seq, entity, op, id, payload, attempts FROM outbox "
                    "WHERE account=? ORDER BY seq LIMIT ?",
                    (acct, int(limit)),
                ).fetchall()
            finally:
                conn.close()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        payload = None
        if r["payload"]:
            try:
                payload = json.loads(r["payload"])
            except json.JSONDecodeError:
                payload = None
        out.append({"seq": r["seq"], "entity": r["entity"], "op": r["op"],
                    "id": r["id"], "payload": payload, "attempts": r["attempts"]})
    return out


def pending_count() -> int:
    acct = _acct()
    if not acct:
        return 0
    try:
        with _lock:
            conn = _conn()
            try:
                return int(conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE account=?", (acct,)).fetchone()[0])
            finally:
                conn.close()
    except sqlite3.Error:
        return 0


def resolve_op(seq: int, ok: bool, error: str = "", server_id: str = "") -> None:
    """上行结果回填。

    成功：丢掉队列项；create/update → biz 标记 synced；delete → 从 biz 真删。
    失败：只累加 attempts 与 last_error，**保留队列项**下次重试。
    server_id：服务端若返回了不同 id（理论上不该发生，因为客户端传了 id），用它修正本地主键。
    """
    acct = _acct()
    if not acct:
        return
    try:
        with _lock:
            conn = _conn()
            try:
                row = conn.execute(
                    "SELECT entity, op, id FROM outbox WHERE seq=? AND account=?",
                    (int(seq), acct),
                ).fetchone()
                if not row:
                    return
                entity, op, item_id = row["entity"], row["op"], row["id"]
                if not ok:
                    conn.execute(
                        "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE seq=?",
                        ((error or "同步失败")[:500], int(seq)),
                    )
                    conn.commit()
                    return
                conn.execute("DELETE FROM outbox WHERE seq=?", (int(seq),))
                if server_id and server_id != item_id:
                    # 服务端返回了不同 id：把本地记录迁到新主键（正常路径不会走到）
                    conn.execute("UPDATE biz SET id=? WHERE account=? AND entity=? AND id=?",
                                 (server_id, acct, entity, item_id))
                    item_id = server_id
                if op == "delete":
                    conn.execute("DELETE FROM biz WHERE account=? AND entity=? AND id=?",
                                 (acct, entity, item_id))
                else:
                    conn.execute(
                        "UPDATE biz SET state='synced' WHERE account=? AND entity=? AND id=?",
                        (acct, entity, item_id),
                    )
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        logger.warning("回填同步结果失败：%s: %s", type(exc).__name__, exc)


def mark_sync_done(result: str = "ok") -> None:
    acct = _acct()
    if not acct:
        return
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT INTO sync_meta(account, last_sync_at, last_result) VALUES(?,?,?) "
                    "ON CONFLICT(account) DO UPDATE SET "
                    "last_sync_at=excluded.last_sync_at, last_result=excluded.last_result",
                    (acct, _now(), (result or "ok")[:200]),
                )
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error:
        pass


def last_sync_at() -> Optional[str]:
    acct = _acct()
    if not acct:
        return None
    try:
        with _lock:
            conn = _conn()
            try:
                row = conn.execute(
                    "SELECT last_sync_at FROM sync_meta WHERE account=?", (acct,)).fetchone()
                return row["last_sync_at"] if row else None
            finally:
                conn.close()
    except sqlite3.Error:
        return None


# ============ 概览 / 清理 ============

def stats() -> Dict[str, Any]:
    """概览：各实体的本地条数与待上行数、队列长度、最近同步时间、库大小。"""
    acct = _acct()
    result: Dict[str, Any] = {
        "account": _account, "entities": [], "outbox": 0,
        "last_sync_at": last_sync_at(), "db_path": _DB_PATH, "db_size": 0,
        "last_error": _last_error,
    }
    try:
        if os.path.exists(_DB_PATH):
            result["db_size"] = os.path.getsize(_DB_PATH)
    except OSError:
        pass
    if not acct:
        return result
    try:
        with _lock:
            conn = _conn()
            try:
                rows = conn.execute(
                    "SELECT entity, "
                    "SUM(CASE WHEN state<>'deleted' THEN 1 ELSE 0 END) AS total, "
                    "SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending "
                    "FROM biz WHERE account=? GROUP BY entity ORDER BY entity",
                    (acct,),
                ).fetchall()
                result["outbox"] = int(conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE account=?", (acct,)).fetchone()[0])
            finally:
                conn.close()
        result["entities"] = [
            {"entity": r["entity"], "total": int(r["total"] or 0),
             "pending": int(r["pending"] or 0)} for r in rows
        ]
    except sqlite3.Error:
        pass
    return result


def clear(account: Optional[str] = None) -> int:
    """清除本地业务数据与队列（默认当前账号；传 "*" 清全部）。返回删除行数。"""
    acct = _acct() if account is None else account
    try:
        with _lock:
            conn = _conn()
            try:
                n = 0
                if acct == "*":
                    for tbl in ("biz", "outbox", "sync_meta"):
                        n += conn.execute(f"DELETE FROM {tbl}").rowcount
                else:
                    n += conn.execute("DELETE FROM biz WHERE account=?", (acct,)).rowcount
                    n += conn.execute("DELETE FROM outbox WHERE account=?", (acct,)).rowcount
                    n += conn.execute("DELETE FROM sync_meta WHERE account=?", (acct,)).rowcount
                conn.commit()
            finally:
                conn.close()
        return n
    except sqlite3.Error:
        return 0
