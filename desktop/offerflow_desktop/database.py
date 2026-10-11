"""SQLite 数据层：建表、模型配置存取、各业务实体的 CRUD。

与 Web 版的数据模型一一对应（去掉多用户 user_id 维度——桌面单机版天然单用户）。
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import DATA_DIR, DB_PATH

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[str] = None
_active_user: Optional[str] = None

# 简单的本地混淆密钥（防随手翻看；单机数据不追求强加密）
_XOR_KEY = b"OfferFlow-Desktop-Local-Key"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _uuid() -> str:
    return uuid.uuid4().hex


def _encrypt(value: str) -> str:
    if not value:
        return ""
    data = value.encode("utf-8")
    xored = bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(data))
    return base64.b64encode(xored).decode("ascii")


def _decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        data = base64.b64decode(value.encode("ascii"))
        return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(data)).decode("utf-8")
    except Exception:
        return ""


def mask_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return api_key[:2] + "****"
    return api_key[:4] + "****" + api_key[-4:]


def _user_db_path(username: Optional[str]) -> str:
    """当前（或指定）用户的业务数据库路径；未登录时用默认库。"""
    if username:
        user_dir = os.path.join(DATA_DIR, "users", username)
        os.makedirs(user_dir, exist_ok=True)
        return os.path.join(user_dir, "offerflow.db")
    return DB_PATH


def get_conn() -> sqlite3.Connection:
    global _conn, _conn_path
    with _lock:
        db_path = _user_db_path(_active_user)
        if _conn is None or _conn_path != db_path:
            if _conn is not None:
                _conn.close()
            _conn = sqlite3.connect(db_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn_path = db_path
        return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    parent_id TEXT,
    raw_text TEXT NOT NULL DEFAULT '',
    parsed_json TEXT,
    source TEXT DEFAULT 'generated',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jds (
    id TEXT PRIMARY KEY,
    raw_text TEXT NOT NULL DEFAULT '',
    company TEXT,
    position TEXT,
    must_have TEXT,
    nice_to_have TEXT,
    tech_stack TEXT,
    hidden_signals TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    jd_id TEXT NOT NULL,
    resume_id TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    skill_match_detail TEXT,
    gap_analysis TEXT,
    suggestion TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(jd_id, resume_id)
);
CREATE TABLE IF NOT EXISTS interview_articles (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL DEFAULT '',
    position TEXT,
    raw_content TEXT NOT NULL DEFAULT '',
    source TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interview_questions (
    id TEXT PRIMARY KEY,
    article_id TEXT,
    question TEXT NOT NULL,
    answer TEXT,
    category TEXT,
    difficulty TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interview_sessions (
    id TEXT PRIMARY KEY,
    resume_id TEXT,
    jd_id TEXT,
    questions TEXT NOT NULL DEFAULT '[]',
    answers TEXT NOT NULL DEFAULT '[]',
    feedbacks TEXT NOT NULL DEFAULT '[]',
    status TEXT DEFAULT 'active',
    summary TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS negotiations (
    id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL DEFAULT 'offer',
    target_salary TEXT,
    bottom_salary TEXT,
    context TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    messages TEXT NOT NULL DEFAULT '[]',
    coaching TEXT NOT NULL DEFAULT '[]',
    round_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    position TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'saved',
    jd_id TEXT,
    resume_id TEXT,
    applied_date TEXT,
    next_action TEXT,
    next_action_date TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_events (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    event_type TEXT DEFAULT 'note',
    from_status TEXT,
    to_status TEXT,
    description TEXT,
    event_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    scope TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        init_users_db()


def _migrate(conn) -> None:
    """轻量迁移：为老库补加新列（幂等，可重复执行）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(resumes)").fetchall()]
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE resumes ADD COLUMN parent_id TEXT")


# ============ 未保存草稿 ============
# 存的是"表单快照"，用于崩溃/关窗后把用户填到一半的内容还回去。
# 刻意**不放进 local_store 的 outbox**：草稿是本机行为，不该上行到云端，
# 也不该被同步队列的合并规则（create 覆盖 update 之类）卷进去。

def save_draft(scope: str, payload: Any) -> bool:
    """写一份草稿（同 scope 覆盖）。返回是否写成功。"""
    scope = str(scope or "").strip()
    if not scope:
        return False
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO drafts(scope, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (scope, blob, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def load_draft(scope: str) -> Optional[Dict[str, Any]]:
    """读草稿，返回 ``{"payload":..., "updated_at":...}``；没有则 None。

    解析失败按"没有草稿"处理并顺手清掉坏行 —— 一份坏 JSON 不该让页面
    每次打开都报一次错。
    """
    scope = str(scope or "").strip()
    if not scope:
        return None
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT payload, updated_at FROM drafts WHERE scope=?", (scope,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        clear_draft(scope)
        return None
    return {"payload": payload, "updated_at": row["updated_at"] or ""}


def clear_draft(scope: str) -> None:
    """删掉一份草稿（保存入库成功、或用户选择丢弃时调用）。"""
    scope = str(scope or "").strip()
    if not scope:
        return
    try:
        conn = get_conn()
        conn.execute("DELETE FROM drafts WHERE scope=?", (scope,))
        conn.commit()
    except sqlite3.Error:
        pass


# ============ 用户体系（元数据库） ============

_USERS_DB_PATH = os.path.join(DATA_DIR, "users.db")


def _users_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_USERS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_users_db() -> None:
    conn = _users_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "username TEXT PRIMARY KEY, "
            "password_hash TEXT NOT NULL, "
            "salt TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    ).hex()


def register_user(username: str, password: str) -> Tuple[bool, str]:
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False, "用户名和密码不能为空"
    if not (2 <= len(username) <= 20):
        return False, "用户名需 2~20 个字符"
    if len(password) < 6:
        return False, "密码至少 6 位"
    init_users_db()
    conn = _users_conn()
    try:
        exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            return False, "用户名已存在"
        salt = secrets.token_hex(16)
        pwd_hash = _hash_password(password, salt)
        conn.execute(
            "INSERT INTO users(username, password_hash, salt, created_at) VALUES(?,?,?,?)",
            (username, pwd_hash, salt, _now()),
        )
        conn.commit()
        return True, "注册成功"
    finally:
        conn.close()


def verify_user(username: str, password: str) -> Tuple[bool, str]:
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False, "请输入用户名和密码"
    conn = _users_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return False, "用户不存在"
        if _hash_password(password, row["salt"]) != row["password_hash"]:
            return False, "密码错误"
        return True, "登录成功"
    finally:
        conn.close()


def set_active_user(username: Optional[str]) -> None:
    """切换当前活跃用户（数据隔离：重连到该用户的独立业务库）。"""
    global _active_user, _conn, _conn_path
    with _lock:
        _active_user = username
        if _conn is not None:
            _conn.close()
            _conn = None
            _conn_path = None
        init_db()


def get_active_user() -> Optional[str]:
    return _active_user


# ============ 系统配置（全局，不随用户隔离） ============

_SYSTEM_CONFIG_PATH = os.path.join(DATA_DIR, "system_config.json")

DEFAULT_SYSTEM_CONFIG = {
    "theme": "light",          # light / dark
    "export_dir": "",          # 空 = 默认（应用目录 exports/）
    "download_dir": "",        # 空 = 默认（系统「下载」文件夹），更新安装包等下载落点
    "auto_start": False,       # 开机自启
    "start_minimized": False,  # 启动时最小化到托盘
    "remembered_username": "",          # 记住的账号（空 = 不记住）
    "remembered_password": "",          # 记住的密码（混淆存储）
    # 导出「外观」：强调色 / 字号缩放 / 中西文字体。与版式**分开**存，
    # 所以用户换版式时配色不会被覆盖（这是拆开两条轴的意义所在）。
    "resume_appearance": {"accent_hex": "", "font_scale": 1.0,
                          "font_cn": "", "font_en": ""},
    # 导出「分区顺序」：core.sections.BODY_SECTIONS 的一个排列。
    # 存下来的是用户显式选择的顺序；为空表示用默认顺序。
    "resume_section_order": [],
}


def get_system_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_SYSTEM_CONFIG)
    try:
        if os.path.exists(_SYSTEM_CONFIG_PATH):
            with open(_SYSTEM_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def set_system_config(**kwargs) -> Dict[str, Any]:
    cfg = get_system_config()
    cfg.update(kwargs)
    try:
        with open(_SYSTEM_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cfg


def resolve_export_dir() -> str:
    """导出文件默认目录：优先系统配置，否则应用目录 exports/。"""
    cfg = get_system_config()
    d = cfg.get("export_dir") or os.path.join(DATA_DIR, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def resolve_download_dir() -> str:
    """下载文件默认目录（更新安装包等）：优先系统配置，否则系统「下载」文件夹。

    默认不再写死任何盘符：用 Windows 已知文件夹 API 取真实「下载」目录
    （可能被用户重定向到非系统盘，正是我们要尊重的），拿不到再回退到
    ~/Downloads，最后回退到 %TEMP%。这样「没有 E 盘」的用户也能正常下载。
    """
    cfg = get_system_config()
    d = (cfg.get("download_dir") or "").strip()
    if not d:
        d = _default_downloads_dir()
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        fallback = os.environ.get("TEMP") or os.path.expanduser("~")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _default_downloads_dir() -> str:
    """系统「下载」文件夹的真实路径（尊重用户的重定向）。非 Windows / 失败回退。"""
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
        fid = GUID(0x374DE290, 0x123F, 0x4565,
                   (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        ptr = ctypes.c_wchar * 260
        p = ctypes.pointer(ptr())
        res = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(fid), 0, None, p)
        if res == 0 and p.contents.value:
            path = p.contents.value
            try:
                ctypes.windll.ole32.CoTaskMemFree(p)
            except Exception:
                pass
            if os.path.isdir(path):
                return path
    except Exception:
        pass
    cand = os.path.join(os.path.expanduser("~"), "Downloads")
    if os.path.isdir(cand):
        return cand
    return os.environ.get("TEMP") or os.path.expanduser("~")


def save_remembered_login(username: str, password: str,
                          remember_user: bool, remember_pwd: bool) -> None:
    """保存「记住我」登录凭据；密码用 XOR 混淆存储（仅本机）。"""
    set_system_config(
        remembered_username=username if remember_user else "",
        remembered_password=_encrypt(password) if remember_pwd else "",
    )


def get_remembered_login() -> Tuple[str, str]:
    """返回 (记住的用户名, 记住的密码)；未记住则为空串。"""
    cfg = get_system_config()
    return cfg.get("remembered_username", ""), _decrypt(cfg.get("remembered_password", ""))


# ============ 模型配置 ============
#
# ⚠️ 这里有**三套**东西，别再混了（2026-10-07 踩过，错题本 E-08）：
#
#   1) 云端账号配置 —— `api_client.get_model_config()` → `/api/v1/user/model-config/`
#      Key 由服务端加密、只回掩码。**「模型设置」页读写的是这一套**，
#      网页版与桌面版共用。走云端的 AI 功能（页面里 `from ... import api_client as db`）
#      由服务器代调模型，与本地配置无关。
#
#   2) `model.*` —— 早期「本地自带 Key」语义（api_key **必填**）。历史遗留，
#      现在只作为 3) 不存在时的兜底。
#
#   3) `local_model.*` —— **本地 AI 专用**（抽取图谱 / 提炼记忆 / `llm.chat`）。
#      判据与云端不同：**api_key 可空** —— 本机 Ollama、vLLM 这类内网地址
#      本来就不需要 Key，而它们恰恰是「本地知识库」最该支持的用法。
#      「模型设置」保存云端成功后会把同一份配置**同步一份到这里**（双写）。
#
# 血泪教训：早先只有 1) 和 2)，而写 2) 的 `save_model_config()` **从来没有任何
# 调用点** ⇒ 本地这份恒为空 ⇒ 图谱/记忆永远报「未配置模型」，而用户在「模型设置」
# 里怎么配都没用（他配的是云端那一份）。**同一概念只要有两套存储，就必须让
# 读端和写端指向同一处。**

MODEL_CONFIG_KEYS = ("provider", "base_url", "model", "api_key_enc")
LOCAL_MODEL_CONFIG_KEYS = ("provider", "base_url", "model", "api_key_enc")


def _read_settings_by_prefix(prefix: str) -> Dict[str, str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?", (prefix + ".%",)
    ).fetchall()
    cut = len(prefix) + 1
    return {r["key"][cut:]: r["value"] for r in rows}


def _write_settings(values: Dict[str, str]) -> None:
    conn = get_conn()
    with _lock:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()


def _drop_settings_by_prefix(prefix: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM settings WHERE key LIKE ?", (prefix + ".%",))
        conn.commit()


def get_local_model_config() -> Dict[str, Any]:
    """本地 AI（图谱 / 记忆 / llm.chat）实际使用的模型配置。

    与云端配置的区别：**api_key 允许为空**（本机 Ollama 等），
    所以判据是「base_url + model 齐备」即可用。
    """
    data = _read_settings_by_prefix("local_model")
    api_key = _decrypt(data.get("api_key_enc", ""))
    base_url = (data.get("base_url") or "").strip()
    model = (data.get("model") or "").strip()
    return {
        "provider": data.get("provider", ""),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "api_key_masked": mask_key(api_key),
        "has_config": bool(base_url and model),
        "source": "local_model",
    }


def save_local_model_config(provider: str, base_url: str, model: str,
                            api_key: str = "") -> None:
    """写本地 AI 用的模型配置。

    ⚠️ 与 `save_model_config` 的关键差异：**api_key 为空也要落库**。
    用户可能从「有 Key 的云端模型」切到「不需要 Key 的本机 Ollama」，
    若沿用「空值不覆盖」的老逻辑，旧的 Key 会一直残留在本地。
    """
    _write_settings({
        "local_model.provider": provider or "",
        "local_model.base_url": (base_url or "").strip().rstrip("/"),
        "local_model.model": (model or "").strip(),
        "local_model.api_key_enc": _encrypt((api_key or "").strip()),
    })


def clear_local_model_config() -> None:
    """清掉本地模型配置（本地 AI 随即回到「未配置」）。"""
    _drop_settings_by_prefix("local_model")


def get_model_config() -> Dict[str, Any]:
    """本地 AI 读的配置：**优先 `local_model.*`**，没有才回退旧的 `model.*`。

    回退分支保留早期语义（api_key 必填），只为兼容既有安装里可能存在的旧数据。
    """
    local = get_local_model_config()
    if local["has_config"]:
        return local

    data = _read_settings_by_prefix("model")
    api_key = _decrypt(data.get("api_key_enc", ""))
    return {
        "provider": data.get("provider", ""),
        "base_url": data.get("base_url", ""),
        "model": data.get("model", ""),
        "api_key": api_key,
        "api_key_masked": mask_key(api_key),
        "has_config": bool(api_key and data.get("base_url") and data.get("model")),
        "source": "model",
    }


def save_model_config(provider: str, base_url: str, model: str, api_key: str) -> None:
    conn = get_conn()
    with _lock:
        values = {
            "model.provider": provider,
            "model.base_url": base_url.rstrip("/"),
            "model.model": model,
        }
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        # 只有用户填写了新 key 才更新（避免掩码/空值覆盖已存密钥）
        if api_key.strip():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("model.api_key_enc", _encrypt(api_key.strip())),
            )
        conn.commit()


# ============ 简历 ============

def save_resume(raw_text: str, parsed_json: Optional[Dict] = None,
                source: str = "generated", parent_id: Optional[str] = None,
                version: int = 1) -> Dict[str, Any]:
    conn = get_conn()
    rid = _uuid()
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO resumes(id, version, parent_id, raw_text, parsed_json, source, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (rid, version, parent_id, raw_text,
             json.dumps(parsed_json, ensure_ascii=False) if parsed_json else None, source, now),
        )
        conn.commit()
    return {"id": rid, "version": version, "raw_text": raw_text, "created_at": now}


def save_resume_version(parent_id: Optional[str], raw_text: str,
                        parsed_json: Optional[Dict] = None) -> Dict[str, Any]:
    """保存优化后的新版本简历：version 在父版本基础上 +1，parent_id 关联上一版。"""
    next_version = 1
    if parent_id:
        parent = get_resume(parent_id)
        if parent:
            next_version = int(parent.get("version") or 1) + 1
    return save_resume(raw_text, parsed_json, source="optimized",
                       parent_id=parent_id, version=next_version)


def list_resumes() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, version, parent_id, raw_text, parsed_json, source, created_at "
        "FROM resumes ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["parsed_json"] = json.loads(item["parsed_json"]) if item["parsed_json"] else None
        result.append(item)
    return result


def get_resume(resume_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, version, parent_id, raw_text, parsed_json, source, created_at FROM resumes WHERE id=?",
        (resume_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["parsed_json"] = json.loads(item["parsed_json"]) if item["parsed_json"] else None
    return item


def delete_resume(resume_id: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM matches WHERE resume_id=?", (resume_id,))
        conn.execute("UPDATE applications SET resume_id=NULL WHERE resume_id=?", (resume_id,))
        conn.execute("DELETE FROM resumes WHERE id=?", (resume_id,))
        conn.commit()


# ============ JD ============

def save_jd(raw_text: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_conn()
    jid = _uuid()
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO jds(id, raw_text, company, position, must_have, nice_to_have, "
            "tech_stack, hidden_signals, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                jid, raw_text,
                parsed.get("company"), parsed.get("position"),
                json.dumps(parsed.get("must_have") or [], ensure_ascii=False),
                json.dumps(parsed.get("nice_to_have") or [], ensure_ascii=False),
                json.dumps(parsed.get("tech_stack") or {}, ensure_ascii=False),
                json.dumps(parsed.get("hidden_signals") or [], ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    return {"id": jid, "raw_text": raw_text, **parsed, "created_at": now}


def list_jds() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM jds ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        item = dict(r)
        for key in ("must_have", "nice_to_have", "hidden_signals"):
            item[key] = json.loads(item[key] or "[]")
        item["tech_stack"] = json.loads(item["tech_stack"] or "{}")
        result.append(item)
    return result


def get_jd(jd_id: str) -> Optional[Dict[str, Any]]:
    for item in list_jds():
        if item["id"] == jd_id:
            return item
    return None


def delete_jd(jd_id: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM matches WHERE jd_id=?", (jd_id,))
        conn.execute("UPDATE applications SET jd_id=NULL WHERE jd_id=?", (jd_id,))
        conn.execute("DELETE FROM jds WHERE id=?", (jd_id,))
        conn.commit()


# ============ 匹配结果 ============

def upsert_match(jd_id: str, resume_id: str, score: int, detail: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_conn()
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO matches(id, jd_id, resume_id, score, skill_match_detail, gap_analysis, "
            "suggestion, created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(jd_id, resume_id) DO UPDATE SET score=excluded.score, "
            "skill_match_detail=excluded.skill_match_detail, gap_analysis=excluded.gap_analysis, "
            "suggestion=excluded.suggestion, created_at=excluded.created_at",
            (
                _uuid(), jd_id, resume_id, score,
                json.dumps(detail.get("skill_match_detail") or {}, ensure_ascii=False),
                json.dumps(detail.get("gap_analysis") or {}, ensure_ascii=False),
                detail.get("suggestion", ""), now,
            ),
        )
        conn.commit()
    return {"jd_id": jd_id, "resume_id": resume_id, "score": score, **detail, "created_at": now}


def list_matches() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.*, j.company AS jd_company, j.position AS jd_position "
        "FROM matches m LEFT JOIN jds j ON m.jd_id = j.id "
        "ORDER BY m.score DESC"
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["skill_match_detail"] = json.loads(item["skill_match_detail"] or "{}")
        item["gap_analysis"] = json.loads(item["gap_analysis"] or "{}")
        item["company"] = item.pop("jd_company")
        item["position"] = item.pop("jd_position")
        result.append(item)
    return result


# ============ 面经 / 面试题 ============

def save_article(company: str, raw_content: str, position: str = "",
                 source: str = "manual") -> Dict[str, Any]:
    conn = get_conn()
    aid = _uuid()
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO interview_articles(id, company, position, raw_content, source, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (aid, company, position, raw_content, source, now),
        )
        conn.commit()
    return {"id": aid, "company": company, "position": position, "raw_content": raw_content, "created_at": now}


def list_articles() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interview_articles ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_article(article_id: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM interview_questions WHERE article_id=?", (article_id,))
        conn.execute("DELETE FROM interview_articles WHERE id=?", (article_id,))
        conn.commit()


def save_generated_questions(questions: List[Dict[str, Any]],
                             article_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    now = _now()
    saved = []
    with _lock:
        for q in questions:
            qid = _uuid()
            conn.execute(
                "INSERT INTO interview_questions(id, article_id, question, answer, category, "
                "difficulty, created_at) VALUES(?,?,?,?,?,?,?)",
                (qid, article_id, q.get("question", ""), q.get("answer", ""),
                 q.get("category", ""), q.get("difficulty", ""), now),
            )
            saved.append({"id": qid, **q})
        conn.commit()
    return saved


def list_questions() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM interview_questions ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# ============ 模拟面试会话 ============

def create_interview_session(resume_id: Optional[str], jd_id: Optional[str]) -> str:
    conn = get_conn()
    sid = _uuid()
    with _lock:
        conn.execute(
            "INSERT INTO interview_sessions(id, resume_id, jd_id, created_at) VALUES(?,?,?,?)",
            (sid, resume_id, jd_id, _now()),
        )
        conn.commit()
    return sid


def update_interview_session(session_id: str, questions: List[str], answers: List[str],
                             feedbacks: List[str], status: Optional[str] = None,
                             summary: Optional[str] = None) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE interview_sessions SET questions=?, answers=?, feedbacks=?, "
            "status=COALESCE(?, status), summary=COALESCE(?, summary) WHERE id=?",
            (json.dumps(questions, ensure_ascii=False), json.dumps(answers, ensure_ascii=False),
             json.dumps(feedbacks, ensure_ascii=False), status, summary, session_id),
        )
        conn.commit()


# ============ 薪资谈判会话 ============

def create_negotiation(scenario: str, target: str, bottom: str, context: str) -> str:
    conn = get_conn()
    nid = _uuid()
    with _lock:
        conn.execute(
            "INSERT INTO negotiations(id, scenario, target_salary, bottom_salary, context, "
            "created_at) VALUES(?,?,?,?,?,?)",
            (nid, scenario, target, bottom, context, _now()),
        )
        conn.commit()
    return nid


def get_negotiation(nid: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM negotiations WHERE id=?", (nid,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["messages"] = json.loads(item["messages"] or "[]")
    item["coaching"] = json.loads(item["coaching"] or "[]")
    return item


def update_negotiation(nid: str, messages: List[Dict], coaching: List[Dict],
                        round_count: int, status: Optional[str] = None) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE negotiations SET messages=?, coaching=?, round_count=?, "
            "status=COALESCE(?, status) WHERE id=?",
            (json.dumps(messages, ensure_ascii=False), json.dumps(coaching, ensure_ascii=False),
             round_count, status, nid),
        )
        conn.commit()


def list_negotiations() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM negotiations ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["messages"] = json.loads(item["messages"] or "[]")
        item["coaching"] = json.loads(item["coaching"] or "[]")
        result.append(item)
    return result


# ============ 投递追踪 ============

def create_application(company: str, position: str, jd_id: Optional[str] = None,
                       resume_id: Optional[str] = None) -> Dict[str, Any]:
    conn = get_conn()
    aid = _uuid()
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO applications(id, company, position, status, jd_id, resume_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (aid, company, position, "saved", jd_id, resume_id, now, now),
        )
        conn.commit()
    return {"id": aid, "company": company, "position": position, "status": "saved", "created_at": now}


def list_applications() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_application(app_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return dict(row) if row else None


def update_application(app_id: str, status: Optional[str] = None,
                       applied_date: Optional[str] = None, next_action: Optional[str] = None,
                       next_action_date: Optional[str] = None,
                       notes: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    with _lock:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            return None
        sets, params = ["updated_at=?"], [_now()]
        if status is not None and status != row["status"]:
            sets.append("status=?")
            params.append(status)
            conn.execute(
                "INSERT INTO application_events(id, application_id, event_type, from_status, "
                "to_status, description, event_date) VALUES(?,?,?,?,?,?,?)",
                (_uuid(), app_id, "status_change", row["status"], status,
                 f"状态变更: {row['status']} → {status}", _now()),
            )
        if applied_date is not None:
            sets.append("applied_date=?"); params.append(applied_date)
        if next_action is not None:
            sets.append("next_action=?"); params.append(next_action)
        if next_action_date is not None:
            sets.append("next_action_date=?"); params.append(next_action_date)
        if notes is not None:
            sets.append("notes=?"); params.append(notes)
        params.append(app_id)
        conn.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
    return get_application(app_id)


def add_application_event(app_id: str, description: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO application_events(id, application_id, event_type, description, event_date) "
            "VALUES(?,?,?,?,?)",
            (_uuid(), app_id, "note", description, _now()),
        )
        conn.commit()


def list_application_events(app_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM application_events WHERE application_id=? ORDER BY event_date DESC",
        (app_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_application(app_id: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM application_events WHERE application_id=?", (app_id,))
        conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        conn.commit()


# ============ 统计 ============

def get_statistics() -> Dict[str, Any]:
    from .config import APPLICATION_STATUSES
    apps = list_applications()
    status_counts = {s: 0 for s in APPLICATION_STATUSES}
    for a in apps:
        status_counts[a["status"]] = status_counts.get(a["status"], 0) + 1

    def reached(app: Dict, status: str) -> bool:
        idx = APPLICATION_STATUSES.index(app["status"]) if app["status"] in APPLICATION_STATUSES else -1
        target = APPLICATION_STATUSES.index(status)
        return idx >= target

    def conversion(from_status: str, to_status: str) -> float:
        from_n = sum(1 for a in apps if reached(a, from_status))
        to_n = sum(1 for a in apps if reached(a, to_status))
        return round(to_n / from_n * 100, 1) if from_n else 0.0

    resumes = list_resumes()
    return {
        "total": len(apps),
        "status_counts": status_counts,
        "conversion_rate": {
            "applied_to_interview": conversion("applied", "first_interview"),
            "interview_to_offer": conversion("first_interview", "offered"),
        },
        "resume_count": len(resumes),
        "optimized_count": sum(1 for r in resumes if r.get("source") == "optimized"),
        "jd_count": len(list_jds()),
        "article_count": len(list_articles()),
    }


def export_all_data() -> Dict[str, Any]:
    """一键导出全部数据（JSON），与 Web 版数据管理对齐。"""
    return {
        "exported_at": _now(),
        "app": "OfferFlow 桌面版",
        "resumes": list_resumes(),
        "jds": list_jds(),
        "matches": list_matches(),
        "interview_articles": list_articles(),
        "interview_questions": list_questions(),
        "negotiations": list_negotiations(),
        "applications": list_applications(),
    }


def import_all_data(data: Dict[str, Any]) -> Dict[str, int]:
    """从 export_all_data 的 JSON 恢复数据（按 id 去重追加），返回各类恢复条数。"""
    conn = get_conn()
    counts: Dict[str, int] = {}
    now = _now()

    def _j(v, default=""):
        return json.dumps(v, ensure_ascii=False) if v not in (None, "", [], {}) else default

    with _lock:
        n = 0
        for r in data.get("resumes", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO resumes(id, version, parent_id, raw_text, parsed_json, source, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (r.get("id"), r.get("version", 1), r.get("parent_id"), r.get("raw_text", ""),
                 json.dumps(r["parsed_json"], ensure_ascii=False) if r.get("parsed_json") else None,
                 r.get("source", "imported"), r.get("created_at", now)),
            ).rowcount
        counts["resumes"] = n

        n = 0
        for j in data.get("jds", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO jds(id, raw_text, company, position, must_have, nice_to_have, "
                "tech_stack, hidden_signals, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (j.get("id"), j.get("raw_text", ""), j.get("company"), j.get("position"),
                 _j(j.get("must_have"), "[]"), _j(j.get("nice_to_have"), "[]"),
                 _j(j.get("tech_stack"), "{}"), _j(j.get("hidden_signals"), "[]"),
                 j.get("created_at", now)),
            ).rowcount
        counts["jds"] = n

        n = 0
        for m in data.get("matches", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO matches(id, jd_id, resume_id, score, skill_match_detail, "
                "gap_analysis, suggestion, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (m.get("id"), m.get("jd_id"), m.get("resume_id"), m.get("score", 0),
                 _j(m.get("skill_match_detail"), "{}"), _j(m.get("gap_analysis"), "{}"),
                 m.get("suggestion", ""), m.get("created_at", now)),
            ).rowcount
        counts["matches"] = n

        n = 0
        for a in data.get("interview_articles", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO interview_articles(id, company, position, raw_content, "
                "source, created_at) VALUES(?,?,?,?,?,?)",
                (a.get("id"), a.get("company", ""), a.get("position"), a.get("raw_content", ""),
                 a.get("source", "manual"), a.get("created_at", now)),
            ).rowcount
        counts["interview_articles"] = n

        n = 0
        for q in data.get("interview_questions", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO interview_questions(id, article_id, question, answer, "
                "category, difficulty, created_at) VALUES(?,?,?,?,?,?,?)",
                (q.get("id"), q.get("article_id"), q.get("question", ""), q.get("answer"),
                 q.get("category"), q.get("difficulty"), q.get("created_at", now)),
            ).rowcount
        counts["interview_questions"] = n

        n = 0
        for neg in data.get("negotiations", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO negotiations(id, scenario, target_salary, bottom_salary, "
                "context, status, messages, coaching, round_count, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (neg.get("id"), neg.get("scenario", "offer"), neg.get("target_salary"),
                 neg.get("bottom_salary"), neg.get("context", ""), neg.get("status", "active"),
                 _j(neg.get("messages"), "[]"), _j(neg.get("coaching"), "[]"),
                 neg.get("round_count", 0), neg.get("created_at", now)),
            ).rowcount
        counts["negotiations"] = n

        n = 0
        for app in data.get("applications", []):
            n += conn.execute(
                "INSERT OR IGNORE INTO applications(id, company, position, status, jd_id, resume_id, "
                "applied_date, next_action, next_action_date, notes, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (app.get("id"), app.get("company", ""), app.get("position", ""),
                 app.get("status", "saved"), app.get("jd_id"), app.get("resume_id"),
                 app.get("applied_date"), app.get("next_action"), app.get("next_action_date"),
                 app.get("notes", ""), app.get("created_at", now), app.get("updated_at", now)),
            ).rowcount
        counts["applications"] = n

        conn.commit()
    return counts
