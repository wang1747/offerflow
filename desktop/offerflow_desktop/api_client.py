"""云端 API 客户端：token 管理、通用请求、登录（桌面版「云端同步」模式）。

桌面版改为微信模式：账号、数据、AI 全部走云端 FastAPI（复用网页版后端）。
本模块逐步提供与 database 模块同签名的函数（内部走 HTTP），
让 UI / services 层以最小改动切换到云端。

统一响应格式（后端 ApiResponse）：{success: bool, data: any, error: str|None}
FastAPI 抛 HTTPException 时返回 {detail: str} 且 status_code >= 400。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests

from . import local_cache, local_store
from .config import DATA_DIR
from .core import salary_benchmark as _bench
from .core import salary_benchmark_store as _bench_store
from .services import interview as _interview
from .services import match as _match
from .services import salary_negotiation as _nego

logger = logging.getLogger(__name__)

# 后端地址：默认本机（`docker compose up` 起的 FastAPI 在 8001）。
# 自建部署时用环境变量覆盖，例如 OFFERFLOW_CLOUD_URL=https://your-host
DEFAULT_CLOUD_URL = os.environ.get("OFFERFLOW_CLOUD_URL", "http://localhost:8001")

# token 持久化文件（登录态跨次启动保留）
_AUTH_PATH = os.path.join(DATA_DIR, "auth.json")

_state: Dict[str, Any] = {
    "base_url": DEFAULT_CLOUD_URL,
    "token": None,
    "user": None,
    # ---- 离线状态：最近一次请求是否「服务器不可达」----
    # True 时界面应提示「离线模式 · 显示本地副本」，而不是把空列表当真实数据。
    "offline": False,
    "offline_reason": "",
    "offline_since": None,      # 首次进入离线的时间（用于展示「数据截至」）
    "offline_until": 0.0,       # 熔断截止时间戳：此前的请求不再真发，直接走副本
    "served_from_cache": False,  # 最近一次读操作是否返回的是本地副本
}
_lock = threading.RLock()

# 读类请求（GET）的正常耗时是毫秒级，没必要沿用 90 秒——断网时那是让用户干等。
_LIST_TIMEOUT = 20
_OP_TIMEOUT = 90      # 写/AI 类请求：生成简历等确实可能跑到一分钟
_OFFLINE_COOLDOWN = 10  # 熔断窗口（秒）：离线后这期间内的请求立即失败并读副本


class ApiError(Exception):
    """云端接口错误（网络失败 / 业务 error / 未登录）。"""


class ApiOfflineError(ApiError):
    """服务器不可达：连接超时、连不上、或网关 5xx。

    单独成类是为了让调用方能分辨「离线」与「服务器明确返回的业务错误」——
    前者可以回退本地副本让用户看到自己的数据，后者不行（例如参数校验失败）。
    """


class ApiAuthError(ApiError):
    """登录已失效（HTTP 401）。

    绝不可回退本地副本：token 已失效，本地副本可能属于上一个账号，
    拿它当「当前用户的数据」显示会造成串号与误导。
    """


def _as_list(v: Any) -> List[Any]:
    """把 None / 非列表安全地变成列表。

    注意不能用 setdefault：键存在但值为 None 时 setdefault 不会替换，
    页面拿到 None 再索引就是 TypeError/KeyError。
    """
    return v if isinstance(v, list) else []


def _as_dict(v: Any) -> Dict[str, Any]:
    """把 None / 非字典安全地变成字典（同上，防 None）。"""
    return v if isinstance(v, dict) else {}


def _norm_ats(v: Any) -> Optional[Dict[str, Any]]:
    """归一优化接口返回的 ATS 体检结果（``ats`` / ``ats_after``）。

    返回 ``None`` 表示**服务端压根没给这份数据**（老版本后端，或这条链在
    早期就断了），它与「给了、但得 0 分」是两回事 —— 页面据此把整块 ATS
    对比收起来，而不是摆一个「0 分」去误导用户。这条与项目里既有的两条
    铁律同源：``match.get`` 绝不重算、Agent 不伪造版本号 —— 宁可不说，
    也不要编一个数字出来。

    ``score`` 非数字/越界同样按「没有数据」处理：服务端自己会做
    ``max(0, min(100, ...))``，走到这里还不合法就说明结构坏了。
    """
    if not isinstance(v, dict):
        return None
    try:
        score = int(v.get("score"))
    except (TypeError, ValueError):
        return None
    if not 0 <= score <= 100:
        return None
    return {
        "score": score,
        "issues": [str(x) for x in _as_list(v.get("issues"))],
        "suggestions": [str(x) for x in _as_list(v.get("suggestions"))],
        "matched_keywords": [str(x) for x in _as_list(v.get("matched_keywords"))],
        "missing_keywords": [str(x) for x in _as_list(v.get("missing_keywords"))],
    }


def _detail_text(detail: Any) -> str:
    """把 FastAPI 的 detail 归一成可直接展示的字符串。

    HTTPException 的 detail 是 str；但 pydantic 校验失败（422）时是
    [{"loc": ["body", "new_password"], "msg": "String should have at least 6 ..."}]，
    直接当字符串用会吐出一堆字典字面量给用户看。
    """
    if isinstance(detail, str):
        return detail.strip()
    if isinstance(detail, list):
        parts: List[str] = []
        for item in detail:
            if isinstance(item, dict):
                loc = [str(x) for x in (item.get("loc") or []) if str(x) != "body"]
                text = str(item.get("msg") or "").strip()
                parts.append(f"{'.'.join(loc)}: {text}" if loc else text)
            else:
                parts.append(str(item))
        return "；".join(p for p in parts if p)
    return str(detail or "").strip()


# ==================== 状态管理 ====================

_http_warn = {"done": False}


def get_base_url() -> str:
    url = _state["base_url"].rstrip("/")
    # 一次性告警：HTTP 下登录凭据与 Bearer Token 明文传输（审计 P1-3）。
    # 不翻转默认值（缺域名/证书会让当前安装包不可用），但至少不让风险静默。
    if not _http_warn["done"]:
        if url.startswith("http://"):
            logger.warning(
                "api_client: 云端地址为明文 HTTP（%s），登录凭据与 Bearer Token 将以"
                "明文传输。生产环境应通过环境变量 OFFERFLOW_CLOUD_URL 切换为 HTTPS。",
                url)
        _http_warn["done"] = True
    return url


def set_base_url(url: str) -> None:
    _state["base_url"] = (url or DEFAULT_CLOUD_URL).rstrip("/")


def get_token() -> Optional[str]:
    return _state["token"]


def get_user() -> Optional[Dict[str, Any]]:
    return _state["user"]


def set_user(user: Optional[Dict[str, Any]]) -> None:
    """更新本地登录态里的用户信息（改名/换头像后调用）。"""
    _state["user"] = user


def get_active_user() -> Optional[str]:
    """返回当前登录账号的显示名（邮箱），未登录为 None。"""
    u = _state.get("user") or {}
    return u.get("email") or u.get("name") or None


def get_active_role() -> str:
    """本机缓存的角色（``admin`` / ``user``），未知返回空串。**不联网**。

    与 ``is_admin_cached()`` 的区别：那个只回答「是不是管理员」，
    这个要的是原始角色名 —— 侧栏用户卡要显示「管理员 / 成员」这类人类可读文案。
    """
    u = _state.get("user") or {}
    return str(u.get("role") or "").lower()


def _account_key(fallback: str = "") -> str:
    """本地镜像缓存用的账号标识。

    优先云端返回的 email（稳定、不重名），退回登录时输入的账号字符串。
    同一台机器换账号登录时，副本按此键隔离，不会串数据。
    """
    u = _state.get("user") or {}
    return (u.get("email") or u.get("name") or fallback or "").strip()


def is_logged_in() -> bool:
    return bool(_state["token"])


def _save_auth() -> None:
    try:
        # 整个 {token, user} 负载加密后落盘，避免 Bearer Token 以明文存储
        # （审计 P2-5）。Windows 走 DPAPI；其他平台 secure_storage 回退明文。
        from . import secure_storage
        payload = json.dumps(
            {"token": _state["token"], "user": _state["user"]},
            ensure_ascii=False)
        blob = secure_storage.protect(payload)
        with open(_AUTH_PATH, "w", encoding="utf-8") as f:
            json.dump({"enc": blob, "v": 1}, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def load_auth() -> None:
    """启动时恢复本地 token（是否仍有效由后续请求判定）。

    兼容两种格式：新格式 {"enc": <base64>} 走 DPAPI 解密；旧明文格式
    {"token":..., "user":...} 直接读取（已有用户不会因升级丢失登录态）。
    """
    try:
        if not os.path.exists(_AUTH_PATH):
            return
        with open(_AUTH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "enc" in data:
            from . import secure_storage
            payload = secure_storage.unprotect(data["enc"])
            parsed = json.loads(payload)
            _state["token"] = parsed.get("token")
            _state["user"] = parsed.get("user")
        else:
            # 旧明文格式：直接读取（向后兼容），但读取后立即重写为 DPAPI 密文，
            # 避免老用户的明文 Token 长期残留在磁盘上（审计复核 P2-5）。
            _state["token"] = data.get("token")
            _state["user"] = data.get("user")
            try:
                _save_auth()
            except Exception:  # noqa: BLE001
                pass
        # 恢复登录态的同时绑定镜像账号，这样离线启动也能读到自己的副本
        if _state["token"]:
            acct = _account_key()
            local_cache.set_account(acct)
            local_store.set_account(acct)
    except Exception:  # noqa: BLE001
        # 解密/解析失败（如跨平台搬运的 DPAPI blob）：清掉损坏的登录态
        _state["token"] = None
        _state["user"] = None


def clear_auth() -> None:
    _state["token"] = None
    _state["user"] = None
    # 解绑镜像账号：登录失效后不该再拿本地副本充数（可能属于上一个账号）
    local_cache.set_account(None)
    local_store.set_account(None)
    _save_auth()


# ==================== 通用请求 ====================

def _mark_offline(reason: str) -> None:
    """记录「服务器不可达」，并开启熔断窗口（窗内不再真发请求）。"""
    with _lock:
        if not _state["offline"]:
            _state["offline_since"] = datetime.now().isoformat(timespec="seconds")
        _state["offline"] = True
        _state["offline_reason"] = reason
        _state["offline_until"] = time.time() + _OFFLINE_COOLDOWN


def _mark_online() -> None:
    """一旦收到服务器的任何 HTTP 响应，就认为链路已恢复。"""
    with _lock:
        _state["offline"] = False
        _state["offline_reason"] = ""
        _state["offline_since"] = None
        _state["offline_until"] = 0.0


def is_offline() -> bool:
    """服务器是否不可达（界面据此显示「离线模式 · 显示本地副本」）。"""
    return bool(_state["offline"])


def offline_reason() -> str:
    return _state["offline_reason"] or ""


def offline_since() -> Optional[str]:
    return _state["offline_since"]


def served_from_cache() -> bool:
    """最近一次读操作返回的是否为本地副本（区别于「服务器上真的没有数据」）。"""
    return bool(_state["served_from_cache"])


def ping(timeout: float = 8) -> bool:
    """轻量探测服务器是否可达（用于界面检测「离线是否已恢复」）。

    故意不走 `_request`：熔断窗口内 `_request` 会直接抛错，
    那样就永远探不到恢复，界面会一直停在离线状态。
    """
    try:
        resp = requests.get(get_base_url() + "/api/health", timeout=timeout)
    except requests.exceptions.RequestException:
        return False
    except Exception:  # noqa: BLE001
        return False
    if resp.status_code < 500:
        _mark_online()
        return True
    return False


def _local_read(entity: str) -> List[Any]:
    """从本地取列表（P4 后本地即主存）。**不改变** served_from_cache 标志。"""
    items = local_store.list_items(entity)
    if items:
        return items
    items = local_cache.read(entity)   # 兼容升级前留下的旧副本
    return items if isinstance(items, list) else []


def _pull_list(entity: str, path: str) -> None:
    """拉服务端列表并落本地（P4 读路径的第一步）。

    失败不抛错 —— 读由调用方统一从本地取（本地永远有可用数据）。
    served_from_cache 仅在**没联系上服务器**时为 True，供界面提示离线。
    """
    try:
        data = _request("GET", path)
    except ApiAuthError:
        # 登录失效：`clear_auth()` 已经把本地主存/副本按账号解绑，调用方随后拿到的是
        # 空列表 —— 这**不是**"离线读副本"，故不能置 served_from_cache，否则界面会
        # 提示"当前离线"，把"掉登录"误导成"网络问题"。
        _state["served_from_cache"] = False
        return
    except (ApiOfflineError, ApiError):
        _state["served_from_cache"] = True
        return
    if isinstance(data, list):
        _mirror(entity, data)          # 内部会置 served_from_cache=False
    else:
        _state["served_from_cache"] = True


def _push_create(seq: int, entity: str, item_id: str, item: Dict[str, Any],
                 path: str, body: Dict[str, Any]) -> None:
    """新建记录的上行。

    成功 → 该记录转 synced；离线 → 安静留在队列（等 30 分钟周期同步）；
    服务端明确拒绝（4xx）→ **撤销本地新建**并抛错（服务端不收，留着只会一直失败）。
    """
    try:
        data = _request("POST", path, data=body)
    except ApiOfflineError:
        logger.info("[%s] %s 已存本地，等待同步（%s）", entity, item_id,
                    _state["offline_reason"] or "离线")
        return
    except ApiError:
        local_store.soft_delete(entity, item_id)
        raise
    sid = str(data.get("id") or "") if isinstance(data, dict) else ""
    local_store.resolve_op(seq, True, server_id=sid)
    if sid and sid != item_id:
        item["id"] = sid


def _cached_list(entity: str) -> List[Any]:
    """离线时取本地记录（旧调用点用；已 P4 化的实体请直接用 `_local_read`）。"""
    _state["served_from_cache"] = True
    return _local_read(entity)


def _cached_dict(entity: str) -> Dict[str, Any]:
    _state["served_from_cache"] = True
    data = local_cache.read(entity)
    return data if isinstance(data, dict) else {}


def _mirror(entity: str, payload: Any) -> None:
    """把一次成功的读结果镜像到本地（失败不影响主流程）。

    离线时直接跳过：此时 payload 本身就是从副本读出来的，
    写回去不仅没意义，还会把 `synced_at` 刷新成「现在」——
    界面上的「数据截至」就会显示一个虚假的、刚更新的时间。

    P4：列表类数据同时写进 `local_store`（可写主存）。下行合并自身会保护本地
    尚未上行的改动（state != synced 的记录不被服务端覆盖），故这里直接写即可。
    """
    _state["served_from_cache"] = False
    if _state["offline"]:
        return
    local_cache.mirror(entity, payload)
    if isinstance(payload, list):
        local_store.replace_synced(entity, payload)


def _index_doc(source: str, source_id: str, text: str, item_type: str) -> None:
    """把用户刚写入的数据**增量**加入本地向量索引（P4 扩展）。

    绝不影响主流程：索引只是增强，写本地主存才是主路径 —— 任何失败只记日志。
    数据本身已在本地主存里，最坏情况是「等下次重建索引才可被检索到」。
    """
    try:
        from .local_ai import index as local_index

        res = local_index.upsert_document(source, source_id, text, item_type=item_type)
        if not res.get("ok"):
            logger.info("[索引] %s:%s 未增量入索引（%s）", source, source_id,
                        res.get("error") or res.get("deferred"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[索引] %s:%s 增量入索引异常：%s: %s",
                       source, source_id, type(exc).__name__, exc)


def _index_drop(source: str, source_id: str) -> None:
    """数据被删除后把它从本地索引摘掉 —— 否则检索还会命中已删内容。"""
    try:
        from .local_ai import index as local_index

        local_index.remove_document(source, source_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[索引] %s:%s 出索引异常：%s: %s",
                       source, source_id, type(exc).__name__, exc)


def _request(method: str, path: str, *, data: Optional[Dict] = None,
             timeout: Optional[float] = None, auth: bool = True) -> Any:
    """调用云端接口，成功返回 data 字段；失败抛 ApiError（子类见 ApiOfflineError/ApiAuthError）。"""
    # 熔断：刚确认离线，窗口内不再真发请求（否则每个页面都要等满超时）。
    # 注意这里**不刷新** offline_until —— 让它自然过期，过期后的那次真实请求
    # 就是「链路是否恢复」的探针；若在窗口内反复刷新，就永远探不到了。
    if _state["offline"] and time.time() < _state["offline_until"]:
        raise ApiOfflineError(_state["offline_reason"] or "服务器不可达")

    if timeout is None:
        timeout = _LIST_TIMEOUT if method.upper() == "GET" else _OP_TIMEOUT

    url = get_base_url() + path
    headers = {"Content-Type": "application/json"}
    if auth and _state["token"]:
        headers["Authorization"] = f"Bearer {_state['token']}"
    try:
        resp = requests.request(method, url, json=data, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        _mark_offline("连接服务器超时")
        raise ApiOfflineError("连接服务器超时，请检查网络") from None
    except requests.exceptions.ConnectionError:
        _mark_offline("无法连接服务器")
        raise ApiOfflineError("无法连接服务器，请检查网络") from None
    except requests.exceptions.RequestException as exc:  # noqa: BLE001
        _mark_offline(str(exc) or "网络请求失败")
        raise ApiOfflineError(f"网络请求失败：{exc}") from None

    # 收到了 HTTP 响应 —— 链路是通的，先解除离线标志
    _mark_online()

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None

    # 网关类错误（502/503/504 等）说明服务端不可用，同样按离线处理：
    # 对用户而言「服务器挂了」和「连不上」的处置方式一致——可读副本、不可写。
    if resp.status_code >= 500:
        _mark_offline(f"服务器错误（HTTP {resp.status_code}）")
        raise ApiOfflineError(f"服务器暂时不可用（HTTP {resp.status_code}），请稍后重试")

    if resp.status_code == 401:
        clear_auth()
        raise ApiAuthError("登录已失效，请重新登录")
    if resp.status_code >= 400:
        msg = ""
        if isinstance(body, dict):
            msg = body.get("detail") or body.get("error") or ""
        raise ApiError(_detail_text(msg) or f"操作失败（HTTP {resp.status_code}）")

    if isinstance(body, dict):
        if body.get("success") is False:
            raise ApiError(body.get("error") or "操作失败")
        return body.get("data")
    return body


def push_op(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    """同步服务专用：把一条待上行操作直接发给云端。

    与 `_request` 的差别只是语义 —— 它**不碰本地主存**，
    避免「同步 → 又写本地 → 又入队」的递归。错误语义与 `_request` 一致。
    """
    return _request(method, path, data=body)


def _upload(path: str, filename: str, file_bytes: bytes,
            timeout: float = 60) -> Any:
    """上传文件（multipart/form-data，字段名固定 file）。复用 _request 的错误语义。"""
    if _state["offline"] and time.time() < _state["offline_until"]:
        raise ApiOfflineError(_state["offline_reason"] or "服务器不可达")

    url = get_base_url() + path
    headers = {}
    if _state["token"]:
        headers["Authorization"] = f"Bearer {_state['token']}"
    try:
        resp = requests.post(url, files={"file": (filename, file_bytes)},
                             headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        _mark_offline("连接服务器超时")
        raise ApiOfflineError("连接服务器超时，请检查网络") from None
    except requests.exceptions.ConnectionError:
        _mark_offline("无法连接服务器")
        raise ApiOfflineError("无法连接服务器，请检查网络") from None
    except requests.exceptions.RequestException as exc:  # noqa: BLE001
        _mark_offline(str(exc) or "网络请求失败")
        raise ApiOfflineError(f"网络请求失败：{exc}") from None

    _mark_online()
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None

    if resp.status_code >= 500:
        _mark_offline(f"服务器错误（HTTP {resp.status_code}）")
        raise ApiOfflineError(f"服务器暂时不可用（HTTP {resp.status_code}），请稍后重试")
    if resp.status_code == 401:
        clear_auth()
        raise ApiAuthError("登录已失效，请重新登录")
    if resp.status_code >= 400:
        msg = ""
        if isinstance(body, dict):
            msg = body.get("detail") or body.get("error") or ""
        raise ApiError(_detail_text(msg) or f"操作失败（HTTP {resp.status_code}）")

    if isinstance(body, dict):
        if body.get("success") is False:
            raise ApiError(body.get("error") or "操作失败")
        return body.get("data")
    return body


def upload_avatar(file_path: str) -> Dict[str, Any]:
    """上传头像，返回最新 UserResponse（含 avatar）。"""
    with open(file_path, "rb") as fh:
        content = fh.read()
    return _upload("/api/v1/auth/avatar", os.path.basename(file_path), content) or {}


def upload_attachment(file_path: str) -> Dict[str, Any]:
    """上传社区附件（图片/Word/PDF），返回 {name,kind,original_name,size,url}。"""
    with open(file_path, "rb") as fh:
        content = fh.read()
    return _upload("/api/v1/community/uploads", os.path.basename(file_path), content) or {}


def download_file(url: str, timeout: float = 10) -> Optional[bytes]:
    """下载文件（头像等，url 可为相对路径），返回 bytes 或 None。"""
    if not url:
        return None
    full = url if url.startswith("http") else get_base_url() + url
    try:
        resp = requests.get(full, timeout=timeout)
        if resp.status_code == 200:
            return resp.content
    except Exception:  # noqa: BLE001
        return None
    return None


# ==================== 认证 ====================

def login(email: str, password: str) -> Tuple[bool, str]:
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False, "请输入邮箱和密码"
    try:
        data = _request("POST", "/api/v1/auth/login",
                        data={"username": email, "password": password}, auth=False)
    except ApiError as exc:
        return False, str(exc)
    if not data or not data.get("access_token"):
        return False, "登录失败：服务器未返回令牌"
    _state["token"] = data["access_token"]
    _state["user"] = data.get("user") or {}
    # 本地镜像按账号隔离，登录成功后立刻绑定（否则副本会写到空账号命名空间里）
    acct = _account_key(email)
    local_cache.set_account(acct)
    local_store.set_account(acct)
    _save_auth()
    return True, "登录成功"


def register(email: str, password: str, name: str = "") -> Tuple[bool, str]:
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False, "请输入邮箱和密码"
    try:
        _request("POST", "/api/v1/auth/register",
                 data={"account": email, "password": password, "name": name}, auth=False)
    except ApiError as exc:
        return False, str(exc)
    return True, "注册成功，请登录"


def logout() -> None:
    try:
        _request("POST", "/api/v1/auth/logout")
    except ApiError:
        pass
    clear_auth()


def me() -> Optional[Dict[str, Any]]:
    """用当前 token 拉取最新用户信息，失败返回 None（token 失效会被 clear_auth）。"""
    try:
        data = _request("GET", "/api/v1/auth/me")
        if isinstance(data, dict):
            _state["user"] = data
            _save_auth()
            return data
    except ApiError:
        pass
    return None


def send_reset_code(account: str, email: str = "") -> Dict[str, Any]:
    """找回密码：向账号邮箱发送 6 位验证码。

    返回 {message, email_masked}。失败抛 ApiError（提示语可直接展示给用户）：
    - 400 请输入正确的邮箱
    - 404 该账号未注册
    - 429 发送过于频繁，请稍后再试
    - 503 邮件服务未配置，请联系管理员
    """
    account = (account or "").strip()
    if not account:
        raise ApiError("请输入注册邮箱")
    data = _request(
        "POST",
        "/api/v1/auth/forgot-password/send-code",
        data={"account": account, "email": (email or "").strip() or None},
        auth=False,
        timeout=60,  # 发送邮件较慢，给足超时
    )
    return _as_dict(data)


def reset_password(account: str, code: str, new_password: str,
                   email: str = "") -> Dict[str, Any]:
    """找回密码：校验验证码并重置密码。

    失败抛 ApiError：
    - 400 请先获取验证码 / 验证码已过期 / 验证码错误 / 新密码长度不足
    - 404 该账号未注册
    """
    account = (account or "").strip()
    new_password = new_password or ""
    if not account:
        raise ApiError("请输入注册邮箱")
    data = _request(
        "POST",
        "/api/v1/auth/forgot-password/reset",
        data={
            "account": account,
            "email": (email or "").strip() or account,  # 后端必填，账号即邮箱
            "code": (code or "").strip(),
            "new_password": new_password,
        },
        auth=False,
    )
    return _as_dict(data)


def send_change_email_code(new_email: str) -> Dict[str, Any]:
    """换绑邮箱：向**新邮箱**发送 6 位验证码（需登录）。

    失败抛 ApiError：400 请输入正确的邮箱 / 新邮箱与当前一致、
    409 该邮箱已被其他账号使用、429 发送过于频繁。
    """
    new_email = (new_email or "").strip()
    if not new_email:
        raise ApiError("请输入新的邮箱")
    data = _request(
        "POST",
        "/api/v1/auth/change-email/send-code",
        data={"new_email": new_email},
        timeout=60,  # 发邮件较慢
    )
    return _as_dict(data)


def change_email(new_email: str, code: str) -> Dict[str, Any]:
    """换绑邮箱：校验验证码后完成换绑（需登录）。

    成功后云端返回最新的用户信息，这里同步更新本地登录态里的邮箱 ——
    否则状态栏、设置页仍显示旧邮箱，且下次「记住账号」会用旧邮箱登录失败。
    """
    new_email = (new_email or "").strip()
    if not new_email:
        raise ApiError("请输入新的邮箱")
    data = _request(
        "POST",
        "/api/v1/auth/change-email",
        data={"new_email": new_email, "code": (code or "").strip()},
    )
    data = _as_dict(data)
    if data.get("email"):
        user = _as_dict(_state.get("user"))
        user["email"] = data["email"]
        _state["user"] = user
        _save_auth()
    return data


# ==================== 投递追踪 application ====================
# 云端前缀 /api/v1/application

def list_applications() -> List[Dict[str, Any]]:
    """投递列表。

    P4：服务端可达时先拉最新数据进本地，然后**统一从本地读** ——
    这样「离线新建、尚未上行」的记录也会出现在列表里
    （旧实现直接返回服务端响应，离线新建的东西根本看不到）。
    """
    _pull_list("applications", "/api/v1/applications/")
    return _local_read("applications")


def create_application(company: str, position: str,
                       jd_id: Optional[str] = None,
                       resume_id: Optional[str] = None) -> Dict[str, Any]:
    """新建投递。

    P4：**先落本地**（立即可见），联网则立刻上行，离线则排队等周期同步 ——
    用户在地铁上也能记投递，不会再被「离线不可写」挡住。
    id 由客户端生成并随请求提交，服务端保证同用户同 id 只建一条（幂等），
    故重复上行不会产生重复数据。
    """
    local_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    item: Dict[str, Any] = {
        "id": local_id, "company": company, "position": position,
        "status": "saved", "jd_id": jd_id, "resume_id": resume_id,
        "applied_date": None, "next_action": None, "next_action_date": None,
        "notes": "", "created_at": now, "updated_at": now,
    }
    seq = local_store.save("applications", "create", local_id, item)
    if seq is None:
        # 未登录（本地主存按账号隔离）→ 走原路径，保持原有报错语义
        data = _request("POST", "/api/v1/applications/",
                        data={"company": company, "position": position,
                              "jd_id": jd_id, "resume_id": resume_id})
        return data if isinstance(data, dict) else {}
    _push_create(seq, "applications", local_id, item, "/api/v1/applications/",
                 {"company": company, "position": position,
                  "jd_id": jd_id, "resume_id": resume_id, "id": local_id})
    return local_store.get_item("applications", local_id) or item


def get_application(app_id: str) -> Optional[Dict[str, Any]]:
    """取单条投递（P4：以本地为主；本地没有才联网刷新一次）。"""
    item = local_store.get_item("applications", app_id)
    if item is not None:
        return item
    _pull_list("applications", "/api/v1/applications/")
    return local_store.get_item("applications", app_id)


def update_application(app_id: str, status: Optional[str] = None,
                       applied_date: Optional[str] = None,
                       next_action: Optional[str] = None,
                       next_action_date: Optional[str] = None,
                       notes: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """更新投递（P4：本地先改，联网则立刻上行，离线排队）。"""
    body: Dict[str, Any] = {}
    if status is not None:
        body["status"] = status
    if applied_date is not None:
        body["applied_date"] = applied_date
    if next_action is not None:
        body["next_action"] = next_action
    if next_action_date is not None:
        body["next_action_date"] = next_action_date
    if notes is not None:
        body["notes"] = notes

    old = local_store.get_item("applications", app_id)
    if old is None:
        data = _request("PUT", f"/api/v1/applications/{app_id}", data=body)
        return data if isinstance(data, dict) else None

    prev = {k: v for k, v in old.items() if k != "_local_state"}
    new = dict(prev)
    new.update(body)
    new["updated_at"] = datetime.now().isoformat(timespec="seconds")
    seq = local_store.save("applications", "update", app_id, new)
    if seq is None:
        data = _request("PUT", f"/api/v1/applications/{app_id}", data=body)
        return data if isinstance(data, dict) else None
    try:
        _request("PUT", f"/api/v1/applications/{app_id}", data=body)
    except ApiOfflineError:
        logger.info("[applications] %s 改动已存本地，等待同步", app_id)
        return new
    except ApiError:
        local_store.restore("applications", app_id, prev)   # 服务端没收 → 本地回滚
        raise
    local_store.resolve_op(seq, True)
    new["_local_state"] = "synced"
    return new


def delete_application(app_id: str) -> None:
    """删除投递（P4：本地先删，联网则立刻上行，离线排队）。

    - 这条若还没上行成功过 → 直接撤销，不发请求；
    - 服务端明确拒绝 → 恢复本地记录并抛错。

    历史坑（语义必须保留）：这里**不能** `except ApiError: pass` ——
    旧版断网时静默成功，用户以为删掉了，其实服务器上还在（刷新一次又出现）。
    """
    old = local_store.get_item("applications", app_id)
    if old is None:
        _request("DELETE", f"/api/v1/applications/{app_id}")
        return
    prev = {k: v for k, v in old.items() if k != "_local_state"}
    seq = local_store.soft_delete("applications", app_id)
    if seq is None:
        return                    # 尚未上行成功的新建 → 已彻底撤销，无需上行
    try:
        _request("DELETE", f"/api/v1/applications/{app_id}")
    except ApiOfflineError:
        logger.info("[applications] 删除 %s 已存本地，等待同步", app_id)
        return
    except ApiError:
        local_store.restore("applications", app_id, prev)
        raise
    local_store.resolve_op(seq, True)


def add_application_event(app_id: str, description: str) -> None:
    _request("POST", f"/api/v1/applications/{app_id}/events",
             data={"event_type": "note", "description": description})


def list_application_events(app_id: str) -> List[Dict[str, Any]]:
    # 云端无独立的事件列表接口，事件已并入投递详情；这里返回空列表兜底
    return []


def get_statistics() -> Dict[str, Any]:
    """首页统计（形状对齐 database.get_statistics）。

    两个坑必须记住：
    1. 路径是 **/api/v1/applications/stats（复数）**。后端前缀就是复数，
       写成单数会 404，再被这里的兜底吞成空 dict，页面取
       conversion_rate['applied_to_interview'] 就 KeyError（首页直接崩）。
    2. 云端 stats 只统计「投递」，不含简历/岗位/面经数量 —— 原来一律补 0，
       导致首页明明有简历也显示 0。这里改用列表接口的真实条数补齐。
    """
    data: Dict[str, Any] = {}
    try:
        got = _request("GET", "/api/v1/applications/stats")
        if isinstance(got, dict):
            data = got
    except ApiOfflineError:
        # 离线：用副本里的统计。下面几个 list_* 调用此时也会走副本，
        # 所以 resume_count / jd_count / article_count 仍然是真实数字。
        data = _cached_dict("statistics")
    except ApiError:
        data = {}

    # conversion_rate：两个键必须存在且可格式化成百分数
    conv = data.get("conversion_rate")
    if not isinstance(conv, dict):
        conv = {}
    for key in ("applied_to_interview", "interview_to_offer"):
        try:
            conv[key] = float(conv.get(key) or 0.0)
        except (TypeError, ValueError):
            conv[key] = 0.0
    data["conversion_rate"] = conv

    counts = data.get("status_counts")
    data["status_counts"] = counts if isinstance(counts, dict) else {}
    try:
        data["total"] = int(data.get("total") or 0)
    except (TypeError, ValueError):
        data["total"] = 0

    # 云端 stats 没有的计数：用列表接口的真实条数补齐
    resumes = list_resumes()
    data["resume_count"] = len(resumes)
    data["jd_count"] = len(list_jds())
    data["article_count"] = len(list_articles())
    # 「优化过」= 版本数 > 1（云端优化会把结果另存为新版本）
    optimized = 0
    for item in resumes:
        try:
            if int(item.get("version_count") or item.get("version") or 1) > 1:
                optimized += 1
        except (TypeError, ValueError):
            pass
    data["optimized_count"] = optimized
    _mirror("statistics", data)
    return data


# ==================== 简历 resume ====================
# 云端前缀 /api/v1/resume；列表只返回元数据（id/version/source_file/created_at）

def _norm_resume(item: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 UI 会读取的键。

    注意 `raw_text` 可能是 NULL，而页面里有 `r["raw_text"][:20]` 这类切片
    → None 会 TypeError，必须兜成空串。
    """
    item["raw_text"] = item.get("raw_text") or ""
    item.setdefault("parsed_json", None)
    item.setdefault("source", item.get("source_file") or "generated")
    return item


def list_resumes() -> List[Dict[str, Any]]:
    """简历列表（P4：先拉服务端并归一落本地，再统一从本地读）。"""
    try:
        data = _request("GET", "/api/v1/resume/list")
        if isinstance(data, list):
            # 存归一后的形状，保证本地记录与联网时结构一致
            _mirror("resumes", [_norm_resume(dict(i)) for i in data if isinstance(i, dict)])
        else:
            _state["served_from_cache"] = True
    except ApiAuthError:
        # 登录失效：clear_auth() 已按账号解绑本地主存，返回空列表而非陈旧副本 ——
        # 这**不是**「离线读副本」，不能置 served_from_cache，否则界面把「掉登录」
        # 误报成「网络问题」（P5 在 _pull_list 里修过同类问题，这里保持一致）。
        _state["served_from_cache"] = False
    except (ApiOfflineError, ApiError):
        _state["served_from_cache"] = True
    return [_norm_resume(dict(i)) for i in _local_read("resumes") if isinstance(i, dict)]


def get_resume(resume_id: str) -> Optional[Dict[str, Any]]:
    """取单份简历。云端没有 GET /resume/{id}，从列表里挑（列表已含 raw_text/parsed_json）。"""
    for item in list_resumes():
        if item.get("id") == resume_id:
            return item
    return None


def upload_resume(raw_text: str, source_file: str = "") -> Dict[str, Any]:
    """导入已有简历（纯文本直传，云端入库），返回 {id, version}。

    后端 `POST /api/v1/resume/upload` 只收 `{raw_text, source_file}`（P4 起还接受可选 `id`）：
    版本号由服务端按用户递增，`parsed_json` 由服务端 `_summarize_resume` 生成，
    客户端**不传**姓名/求职意向（服务端不接收该字段）。

    P4：**先落本地**（离线也能导入），联网则立刻上行，离线排队等同步。
    id 由客户端生成，服务端同用户同 id 幂等 → 重复上行不会产生重复简历。

    坑：旧版这里调的是本地 SQLite 的 `database.save_resume(raw_text, parsed, source)`，
    迁到云端后 `db` 已指向 api_client，导致 `AttributeError` —— 「导入简历」一点就崩，
    简历根本进不了库。改动时务必用 `_request` 走云端。
    """
    text = (raw_text or "").strip()
    if len(text) < 30:
        raise ApiError("提取到的简历内容太短（少于 30 字），请确认文件是否完整")
    payload = {"raw_text": text,
               "source_file": (source_file or "").strip() or None}

    local_id = uuid.uuid4().hex
    item: Dict[str, Any] = {
        "id": local_id, "raw_text": text,
        "source_file": (source_file or "").strip() or "",
        "source": "imported", "version": None, "parsed_json": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    seq = local_store.save("resumes", "create", local_id, item)
    if seq is None:
        data = _request("POST", "/api/v1/resume/upload", data=payload)
        d = _as_dict(data)
        if not d.get("id"):
            raise ApiError("导入失败：服务端未返回简历编号")
        return d
    # 刚导入的简历立刻进本地索引（离线也能被「生成简历 / 面试准备」参考到）
    _index_doc("resume", local_id, text, "resume")
    try:
        data = _request("POST", "/api/v1/resume/upload", data={**payload, "id": local_id})
    except ApiOfflineError:
        logger.info("[resumes] 导入的简历已存本地，等待同步")
        return local_store.get_item("resumes", local_id) or item
    except ApiError:
        local_store.soft_delete("resumes", local_id)   # 服务端没收 → 撤销本地新建
        _index_drop("resume", local_id)
        raise
    d = _as_dict(data)
    if not d.get("id"):
        local_store.soft_delete("resumes", local_id)
        _index_drop("resume", local_id)
        raise ApiError("导入失败：服务端未返回简历编号")
    if d.get("version") is not None:
        item["version"] = d["version"]
    local_store.resolve_op(seq, True, server_id=str(d.get("id") or ""))
    local_store.restore("resumes", local_id, item)     # 把服务端版本号写回本地（state=synced）
    return local_store.get_item("resumes", local_id) or item


def delete_resume(resume_id: str) -> None:
    """删除简历（P4：本地先删，联网则立刻上行，离线排队；异常交给 UI 提示，不静默吞掉）。"""
    old = local_store.get_item("resumes", resume_id)
    if old is None:
        _request("DELETE", f"/api/v1/resume/{resume_id}")
        return
    prev = {k: v for k, v in old.items() if k != "_local_state"}
    seq = local_store.soft_delete("resumes", resume_id)
    _index_drop("resume", resume_id)
    if seq is None:
        return                    # 尚未上行成功的新建 → 已彻底撤销，无需上行
    try:
        _request("DELETE", f"/api/v1/resume/{resume_id}")
    except ApiOfflineError:
        logger.info("[resumes] 删除 %s 已存本地，等待同步", resume_id)
        return
    except ApiError:
        local_store.restore("resumes", resume_id, prev)
        _index_doc("resume", resume_id, prev.get("raw_text") or "", "resume")
        raise
    local_store.resolve_op(seq, True)


def optimize_resume(resume_id: str, jd_id: str) -> Dict[str, Any]:
    """优化简历（云端优化并自动保存新版本），返回优化结果（optimized 等字段）。"""
    data = _request("POST", "/api/v1/resume/optimize",
                    data={"resume_id": resume_id, "jd_id": jd_id})
    if isinstance(data, dict):
        # 云端字段名是 optimized，原生页面用 optimized_text
        data["optimized_text"] = data.get("optimized") or data.get("optimized_text") or ""
        data["gap"] = _as_dict(data.get("gap"))
        data["preservation"] = _as_dict(data.get("preservation"))
        data["added_keywords"] = _as_list(data.get("added_keywords"))
        data["removed"] = _as_list(data.get("removed"))
        data["length_warning"] = data.get("length_warning") or ""
        # ATS 前后对比：服务端在优化返回里同时给了 ats / ats_after 两份体检
        # 结果（同一份 check_ats_compatibility，分别跑「原文」与「优化稿」，
        # 所以两次分数可比）。这里只做归一化，**不在本地重算**。
        data["ats"] = _norm_ats(data.get("ats"))
        data["ats_after"] = _norm_ats(data.get("ats_after"))
        return data
    return {}


# ==================== 岗位 JD ====================
# 云端前缀 /api/v1/jd

def _norm_jd(item: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 UI 会直接读取的键。

    `/jd/parse` 把 LLM 解析出的 `parsed` 透传回来，字段**不保证齐全**
    （模型可能少给 must_have / tech_stack），而 jd_analysis 页面会直接
    `jd["must_have"]` / `jd["hidden_signals"]` —— 必须在这里补齐类型。
    列表接口同理：键在但值为 None（数据库列可为 NULL）也会让页面崩。
    """
    item["raw_text"] = item.get("raw_text") or ""
    item["company"] = item.get("company") or ""
    item["position"] = item.get("position") or ""
    item["must_have"] = _as_list(item.get("must_have"))
    item["nice_to_have"] = _as_list(item.get("nice_to_have"))
    item["hidden_signals"] = _as_list(item.get("hidden_signals"))
    item["tech_stack"] = _as_dict(item.get("tech_stack"))
    return item


def list_jds() -> List[Dict[str, Any]]:
    """岗位 JD 列表（P4：先拉服务端并归一落本地，再统一从本地读）。

    统一从本地读的理由：离线新建、还没上行成功的 JD 也在本地主存里，
    旧实现直接返回服务端响应 → 用户刚存的 JD 在列表里看不到。
    """
    try:
        data = _request("GET", "/api/v1/jd/list")
        if isinstance(data, list):
            _mirror("jds", [_norm_jd(dict(i)) for i in data if isinstance(i, dict)])
        else:
            _state["served_from_cache"] = True
    except ApiAuthError:
        _state["served_from_cache"] = False    # 掉登录 ≠ 离线（见 _pull_list 注释）
    except (ApiOfflineError, ApiError):
        _state["served_from_cache"] = True
    return [_norm_jd(dict(i)) for i in _local_read("jds") if isinstance(i, dict)]


def get_jd(jd_id: str) -> Optional[Dict[str, Any]]:
    for item in list_jds():
        if item.get("id") == jd_id:
            return item
    return None


def save_jd(raw_text: str, parsed: Optional[Dict] = None) -> Dict[str, Any]:
    """解析并保存 JD，返回完整 jd dict（页面直接拿去渲染）。

    P4：**先落本地**（离线也能把 JD 原文存下来），联网则立刻上行。
    服务端的 LLM 解析在离线时无法完成 —— 此时本地记录 `parsed_pending=True`，
    页面会如实提示「已保存，联网后自动解析」，而不是假装解析成功。
    （旧实现离线直接抛错，用户粘好的 JD 原文白丢。）

    `parsed` 形参只为兼容旧调用保留：解析一律以服务端结果为准。
    """
    text = (raw_text or "").strip()
    if not text:
        raise ApiError("JD 原文为空，无法保存")

    local_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    item: Dict[str, Any] = {
        "id": local_id, "raw_text": text, "company": "", "position": "",
        "must_have": [], "nice_to_have": [], "hidden_signals": [], "tech_stack": {},
        "created_at": now, "parsed_pending": True,
    }
    seq = local_store.save("jds", "create", local_id, item)
    if seq is None:
        # 未登录（本地主存按账号隔离）→ 走原路径，保持原有报错语义
        data = _request("POST", "/api/v1/jd/parse", data={"raw_text": text})
        result: Dict[str, Any] = {"raw_text": text}
        if isinstance(data, dict):
            result["id"] = data.get("id")
            result.update(_as_dict(data.get("parsed")))
        return _norm_jd(result)

    _index_doc("jd", local_id, text, "jd")
    try:
        data = _request("POST", "/api/v1/jd/parse", data={"raw_text": text, "id": local_id})
    except ApiOfflineError:
        logger.info("[jds] JD 已存本地，等待联网后自动解析并同步")
        return local_store.get_item("jds", local_id) or item
    except ApiError:
        local_store.soft_delete("jds", local_id)     # 服务端没收 → 撤销本地新建
        _index_drop("jd", local_id)
        raise

    merged = dict(item)
    if isinstance(data, dict):
        merged.update(_as_dict(data.get("parsed")))
        merged["id"] = str(data.get("id") or local_id)
    merged["parsed_pending"] = False
    local_store.resolve_op(seq, True, server_id=merged["id"])
    local_store.restore("jds", merged["id"], merged)   # 解析结果写回本地（state=synced）
    if merged["id"] != local_id:
        _index_drop("jd", local_id)
        _index_doc("jd", merged["id"], text, "jd")
    return local_store.get_item("jds", merged["id"]) or merged


def delete_jd(jd_id: str) -> None:
    """删除 JD（P4：本地先删，联网则立刻上行，离线排队）。

    语义必须保留：这里**不能** `except ApiError: pass` ——
    旧版断网时静默成功，用户以为删掉了，其实服务器上还在（刷新一次又出现）。
    """
    old = local_store.get_item("jds", jd_id)
    if old is None:
        _request("DELETE", f"/api/v1/jd/{jd_id}")
        _index_drop("jd", jd_id)
        return
    prev = {k: v for k, v in old.items() if k != "_local_state"}
    seq = local_store.soft_delete("jds", jd_id)
    _index_drop("jd", jd_id)
    if seq is None:
        return                    # 尚未上行成功的新建 → 已彻底撤销，无需上行
    try:
        _request("DELETE", f"/api/v1/jd/{jd_id}")
    except ApiOfflineError:
        logger.info("[jds] 删除 %s 已存本地，等待同步", jd_id)
        return
    except ApiError:
        local_store.restore("jds", jd_id, prev)   # 服务端拒 → 本地回滚，索引也补回
        _index_doc("jd", jd_id, prev.get("raw_text") or "", "jd")
        raise
    local_store.resolve_op(seq, True)


def update_jd(jd_id: str, company: Optional[str] = None,
              position: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """修改 JD 的公司 / 职位（问题修复：此前 JD 无任何改名入口）。

    本地优先：先写本地主存（立即生效，UI 列表随之刷新），再尝试上行服务端
    ``PUT /api/v1/jd/{jd_id}``。 ``company`` / ``position`` 传 ``None`` 表示
    不改动该字段；传空串 ``""`` 表示**清空**该字段（与服务端 ``JDUpdateRequest``
    的「非 None 即写入」语义一致）。

    离线或服务端拒绝都**不**回滚本地改动 —— 改名是用户主动纠正，本地必须立刻可见，
    同步问题交给后台 outbox（同 save/delete 的本地优先模型）。
    """
    if company is None and position is None:
        raise ApiError("update_jd 至少需要一个要修改的字段（company / position）")
    item = local_store.get_item("jds", jd_id) or {}
    merged: Dict[str, Any] = {k: v for k, v in item.items() if k != "_local_state"}
    if company is not None:
        merged["company"] = (company or "").strip()
    if position is not None:
        merged["position"] = (position or "").strip()
    merged["id"] = jd_id
    # 本地落库：op="update" 入 outbox，联网后自动上行（若仍是 pending create 则保 create）。
    seq = local_store.save("jds", "update", jd_id, merged)
    if seq is None:
        # 未登录（本地主存按账号隔离）：没法落本地，直接走服务端路径。
        try:
            data = _request("PUT", f"/api/v1/jd/{jd_id}",
                            data={"company": merged["company"],
                                  "position": merged["position"]})
            return _as_dict(data.get("data")) or merged
        except ApiOfflineError:
            raise ApiError("未登录且离线：无法保存改名，请先登录或联网")
        except ApiError as exc:
            raise ApiError(f"改名失败：{exc}")
    # 本地已落库，立即对 UI 生效；上行尽力而为。
    try:
        _request("PUT", f"/api/v1/jd/{jd_id}",
                 data={"company": merged["company"], "position": merged["position"]})
        local_store.resolve_op(seq, True, server_id=jd_id)
    except ApiOfflineError:
        logger.info("[jds] JD %s 改名已存本地，等待同步", jd_id)
    except ApiError as exc:
        # 服务端拒（如 404 已被别人删）：保留本地改动，但提示同步未成功。
        logger.warning("[jds] JD %s 改名本地已存，上行失败：%s", jd_id, exc)
    return merged


# ==================== 岗位匹配 match ====================
# **本地计算**：打分走 services.match（技能词典 × LLM 语义判定），结果落本机。
# 匹配结果**不上行服务器** —— 理由见 calculate_match 的 docstring。

_MATCH_ENTITY = "matches"


def _match_key(jd_id: str, resume_id: str) -> str:
    """匹配结果的本地主键。

    服务端的 `match_results` 隐式按 (jd_id, resume_id) 唯一（`_persist_match` 就是
    按这两列查已有记录的），桌面版把它显式化成字符串主键，一次匹配只留一条记录。
    """
    return f"{jd_id}:{resume_id}"


def _norm_match(item: Dict[str, Any]) -> Dict[str, Any]:
    """补齐排行表直接读取的键（老数据可能缺字段）。"""
    item["company"] = item.get("company") or ""
    item["position"] = item.get("position") or ""
    item["suggestion"] = item.get("suggestion") or ""
    try:
        item["score"] = int(item.get("score") or 0)
    except (TypeError, ValueError):
        item["score"] = 0
    return item


def _save_match_result(jd: Dict[str, Any], resume: Dict[str, Any],
                       result: Dict[str, Any]) -> None:
    """把一次匹配结果写本机主存（供「匹配排行」离线可读）。

    `state="synced"` ⇒ **不进 outbox**：匹配是「随时可重算的派生数据」，
    上行只会让服务端用它的 Chroma 口径重算一遍、再在下行时把本地分数覆盖掉，
    用户会看到「同一个岗位的分数自己变了」。未登录时写入失败也不影响本次结果展示，
    只影响排行里有没有它 —— 记一条 debug 即可，不打扰用户。
    """
    jd_id, resume_id = str(jd.get("id") or ""), str(resume.get("id") or "")
    if not jd_id or not resume_id:
        return
    mid = _match_key(jd_id, resume_id)
    item = {
        "id": mid,
        "jd_id": jd_id,
        "resume_id": resume_id,
        "company": jd.get("company") or "",
        "position": jd.get("position") or "",
        "score": result.get("score") or 0,
        "skill_match_detail": result.get("skill_match_detail") or {},
        "gap_analysis": result.get("gap_analysis") or {},
        "suggestion": result.get("suggestion") or "",
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not local_store.restore(_MATCH_ENTITY, mid, item, state="synced"):
        logger.debug("[matches] 本次匹配结果未落户本地（未登录或本地库不可用）：%s", mid)


def calculate_match(jd_id: str, resume_id: str) -> Dict[str, Any]:
    """计算匹配度（**本机计算**），并把结果写本机供排行使用。

    打分口径与 Web 版一致（技能词典 × 语义覆盖度），但语义判定由**本机模型**完成，
    模型不可用时自动降级到本地规则 `analyze_gap`，不会因为没配 Key 就整个功能不可用。

    返回结构与云端 `MatchResponse` 对齐：`score` / `skill_match_detail` /
    `gap_analysis` / `suggestion`，另外多带 `requirement_detail` / `evidence` / `stats`
    （逐条判定与证据句，供界面做「为什么是这个分」的解释）。
    """
    resume = get_resume(resume_id)
    jd = get_jd(jd_id)
    if not resume:
        raise ApiError("简历不存在")
    if not jd:
        raise ApiError("JD 不存在")

    result = _match.calculate_match(jd, resume)
    result.setdefault("jd_id", jd_id)
    result.setdefault("resume_id", resume_id)
    _save_match_result(jd, resume, result)
    return result


def list_matches() -> List[Dict[str, Any]]:
    """匹配排行：**本机记录优先**，并合并云端 rankings（按 jd:resume 去重）。

    为什么要合并两边：本机算的结果不上行（见 calculate_match），云端又可能存着
    用户在**网页版**算过的结果 —— 只读一边都会漏。同一个 (jd, resume) 两边都有时
    **以本机为准**（用户最近一次看的就是本机算出来的那个分）。
    云端不可达/未登录时静默降级为「只有本机记录」，不弹错。
    """
    merged: Dict[str, Dict[str, Any]] = {}

    try:
        data = _request("GET", "/api/v1/match/rankings")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = _match_key(str(item.get("jd_id") or ""), str(item.get("resume_id") or ""))
                merged[key] = _norm_match(dict(item))
    except ApiAuthError:
        _state["served_from_cache"] = False    # 掉登录 ≠ 离线
    except (ApiOfflineError, ApiError) as exc:
        logger.info("[matches] 云端排行不可用，只用本机记录：%s", exc)

    for item in _local_read(_MATCH_ENTITY):
        if not isinstance(item, dict):
            continue
        key = _match_key(str(item.get("jd_id") or ""), str(item.get("resume_id") or ""))
        merged[key] = _norm_match(dict(item))   # 本机覆盖云端

    return sorted(merged.values(), key=lambda m: -(m.get("score") or 0))


# ==================== 薪资谈判 salary ====================
# **本地计算 + 本地会话**：LLM 走本机模型，基准数据存本机（core/salary_benchmark_store）。


def start_negotiation(scenario: str, target: str, bottom: str, context: str) -> Dict[str, Any]:
    """开始薪资谈判（本机生成 HR 开场白），返回 `{session_id, hr_message, round_count}`。"""
    session = _nego.create_session(scenario, target, bottom, context)
    return {
        "session_id": session.get("id"),
        "hr_message": session.get("current_hr_message") or "",
        "round_count": session.get("round_count") or 0,
    }


def salary_reference(resume_id: Optional[str], city: str, position: str) -> Dict[str, Any]:
    """薪资参考（**本机计算**：规则查表 + 本机模型提画像/写解读）。

    数字全部由 `core.salary_benchmark` 按「学历 × 方向 × 城市档 × 经验」算出，
    LLM 只负责读简历提画像、以及解释「为什么是这个价」。
    """
    resume_text = ""
    if resume_id:
        resume = get_resume(resume_id)
        if not resume:
            raise ApiError("简历不存在")
        resume_text = str(resume.get("raw_text") or "")
    if not resume_text.strip():
        raise ApiError("简历内容为空，无法估算市场价")
    return _nego.get_salary_reference(resume_text, city or "", position or "")


def salary_benchmark_info() -> Dict[str, Any]:
    """当前生效的薪资基准版本信息（**本机**：version / data_year / source / note / updated_at）。

    ⚠️ 与 Web 版**不共享**同一份基准数据（桌面端没有拿到服务端数据表的通道，
    详见 `core/salary_benchmark_store` 的模块 docstring）。本机从没导入过时
    返回 `version=0 / source="内置基线"`，界面据此显示「还没导入过权威数据」。
    """
    return _bench_store.info()


def salary_benchmark_refresh() -> Dict[str, Any]:
    """刷新薪资基准数据 —— **固定拒绝**，与 Web 版行为一致。

    Web 版已把这条路改成硬拒绝（模型估算会编造数字污染真实数据），
    `salary_benchmark_refresh` 里的 400 说明本身就是给用户看的指引：
    改用「导入」写入权威调研数据。桌面版照抄这个行为，不提供任何「一键刷新」假象。
    """
    raise ApiError("薪资基准已改为联网采集真实行情更新，不再使用模型估算，请通过「导入」写入权威数据")


def salary_benchmark_import(data_json: Dict[str, Any], data_year: str = "",
                            source: str = "", note: str = "") -> Dict[str, Any]:
    """导入权威调研数据作为本机生效版本，返回写入后的版本信息。

    ⚠️ 与 Web 版的关键差异：**只写本机**，不影响其他账号，也不上传服务器
    （服务端基准表是全局的，自动上行会改动所有账号的市场价计算 —— 代价远超收益）。
    数据经 `salary_benchmark.sanitize_data` 校验，不合法抛 `ApiError` 并**不写入任何东西**。

    返回体在版本信息之外多带 `data_report`（`unknown_keys` / `replaced_keys` /
    `missing_keys` 三桶）：`sanitize_data` 会把不认识的字段名丢弃、把非法值或缺失
    字段换成内置默认值，不说清楚的话用户会看到「已导入到 v2」却发现数字一个都没变。
    `unknown_keys` 单独保留一个平铺键，供旧界面读取（向后兼容）。
    """
    report: Dict[str, Any] = {}
    try:
        _bench_store.refresh(data_json, data_year=data_year, source=source, note=note,
                             report=report)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    info = dict(_bench_store.info())
    unknown = report.get("unknown_keys") or _bench.unknown_keys(data_json)
    if unknown:
        report["unknown_keys"] = unknown
    if any(report.get(bucket) for bucket in ("unknown_keys", "replaced_keys", "missing_keys")):
        logger.warning("导入的基准数据有字段未按所给值生效：%s", report)
        info["data_report"] = report
        if report.get("unknown_keys"):
            info["unknown_keys"] = report["unknown_keys"]      # 向后兼容旧界面
    return info


def submit_negotiation_answer(session_id: str, answer: str) -> Dict[str, Any]:
    """提交一轮回应，返回 `{coaching, next_hr_message, is_finished, round_count}`。"""
    coaching, next_hr, finished, error = _nego.submit_answer(session_id, answer)
    if coaching is None and next_hr is None and not finished:
        raise ApiError(error or "会话不存在")
    return {
        "coaching": coaching or "",
        "next_hr_message": next_hr,
        "is_finished": finished,
        "round_count": (_nego.get_session(session_id) or {}).get("round_count") or 0,
        "error": error,
    }


def get_negotiation_summary(session_id: str) -> Dict[str, Any]:
    """整场谈判总结（本机生成）。仅当结束/总结时才调用模型。"""
    session = _nego.get_session(session_id)
    if not session:
        raise ApiError("会话不存在")
    return _nego.summary_of(session)


# ==================== 面试 interview ====================
# 面经 CRUD：本机主存 + 同步（P4）；题目生成与模拟面试：**本机 LLM**。

def _norm_article(item: Dict[str, Any]) -> Dict[str, Any]:
    """补齐面经页面会直接读取的键（列表字段可为 None）。"""
    item["company"] = item.get("company") or ""
    item["position"] = item.get("position") or ""
    item["raw_content"] = item.get("raw_content") or ""
    item["questions"] = _as_list(item.get("questions"))
    item.setdefault("source", "manual")
    item["difficulty"] = item.get("difficulty") or ""
    item["tags"] = _as_list(item.get("tags"))
    return item


_ARTICLE_PAGE_SIZE = 100     # 服务端 page_size 上限
_ARTICLE_MAX_PAGES = 50      # 兜底：防止 total 异常导致死循环


def list_articles() -> List[Dict[str, Any]]:
    """面经列表（P4：**翻完全部分页**落本地，再统一从本地读）。

    为什么要翻页而不是只拉第一页：
      1. 接口是分页的（page_size 上限 100），只取首页会漏掉后面的面经；
      2. 更严重 —— 下行合并 `replace_synced` 会把「服务端这批里没有的」本地记录
         删掉，只拉首页等于**把第 2 页往后的面经从本地直接删干净**。
    """
    items: List[Dict[str, Any]] = []
    page = 1
    try:
        while page <= _ARTICLE_MAX_PAGES:
            data = _request(
                "GET",
                f"/api/v1/interview/articles?page={page}&page_size={_ARTICLE_PAGE_SIZE}",
            )
            if not isinstance(data, dict):
                break
            batch = [_norm_article(dict(i)) for i in (data.get("items") or [])
                     if isinstance(i, dict)]
            items.extend(batch)
            total = int(data.get("total") or 0)
            if not batch or len(items) >= total:
                break
            page += 1
        _mirror("articles", items)
    except ApiAuthError:
        _state["served_from_cache"] = False    # 掉登录 ≠ 离线
    except (ApiOfflineError, ApiError) as exc:
        logger.info("[articles] 拉取面经失败，改用本地记录：%s", exc)
        _state["served_from_cache"] = True
    return [_norm_article(dict(i)) for i in _local_read("articles") if isinstance(i, dict)]


def save_article(company: str, raw_content: str, position: str = "",
                 source: str = "manual") -> Dict[str, Any]:
    """导入面经（P4：先落本地，联网则立刻上行；服务端自动去重 + 抽题打标）。

    抽题/打标现在**本地也能做**：本机模型可用时就地抽一次写进本地记录，
    断网/未配模型时留空（`metadata_pending=True`），联网同步后由服务端补齐。
    这样「断网导入面经 → 看不到题目」这个体验缺口被补上了。

    返回体带 `queued=True` 表示「已存本地待同步」，`duplicate=True` 表示判定为重复
    （联网时由服务端判、离线时由本机按同一套相似度判据判）。
    """
    text = (raw_content or "").strip()
    if not (company or "").strip() or not text:
        raise ApiError("公司与面经内容不能为空")

    # 离线去重：服务端那套（规范化文本相似度 >= 0.9）在本地也跑一遍。
    # 不做的话，断网时反复导入同一份面经会在本地堆出多条一模一样的记录。
    existing = list_articles()
    dup = _interview.find_duplicate_article(text, existing)
    if dup and dup.get("id"):
        out = dict(dup)
        out["duplicate"] = True
        return out

    local_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    item: Dict[str, Any] = {
        "id": local_id, "company": company, "position": position or "",
        "raw_content": text, "source": source, "questions": [], "tags": [],
        "difficulty": "", "created_at": now, "metadata_pending": True,
    }
    seq = local_store.save("articles", "create", local_id, item)
    if seq is None:
        data = _request("POST", "/api/v1/interview/articles",
                        data={"company": company, "position": position,
                              "raw_content": text, "source": source})
        return data if isinstance(data, dict) else {}

    _index_doc("article", local_id, text, "interview")
    try:
        data = _request("POST", "/api/v1/interview/articles",
                        data={"company": company, "position": position,
                              "raw_content": text, "source": source, "id": local_id})
    except ApiOfflineError:
        # 断网：抽题这一步在服务端做不了，改由本机补上 —— 否则用户在离线导入后
        # 只能看到一条光秃秃的原文，题目/标签/难度全空。
        merged = _extract_article_metadata_locally(item)
        out = dict(merged)
        out["duplicate"] = False
        out["queued"] = True
        logger.info("[articles] 面经已存本地（含本机抽题），等待同步")
        return out
    except ApiError:
        local_store.soft_delete("articles", local_id)   # 服务端没收 → 撤销本地新建
        _index_drop("article", local_id)
        raise

    d = _as_dict(data)
    if d.get("duplicate"):
        # 服务端判定与已有面经重复：本地这条新建**必须撤销** —— 否则本地会多出一条
        # 服务器上并不存在的面经，而且每次同步都会被判重复、永远清不掉。
        local_store.soft_delete("articles", local_id)
        _index_drop("article", local_id)
        return d

    sid = str(d.get("id") or local_id)
    merged = dict(item)
    merged["id"] = sid
    for key in ("questions", "tags", "difficulty"):
        if d.get(key) is not None:
            merged[key] = d[key]
    merged["metadata_pending"] = False
    local_store.resolve_op(seq, True, server_id=sid)
    local_store.restore("articles", sid, merged)      # 抽题结果写回本地（state=synced）
    if sid != local_id:
        _index_drop("article", local_id)
        _index_doc("article", sid, text, "interview")
    out = dict(merged)
    out["duplicate"] = False
    out["question_count"] = len(merged.get("questions") or [])
    return out


def _extract_article_metadata_locally(item: Dict[str, Any]) -> Dict[str, Any]:
    """离线导入面经时用本机模型补抽题/打标/难度；失败保持原样（不改坏元数据）。

    抽不出来**不算错误**：面经原文已经存好了，题目可以等联网后由服务端补。
    这里只记一条日志，返回的 item 保持 `metadata_pending=True`。
    """
    merged = dict(item)
    try:
        meta = _interview.extract_article_metadata(merged.get("raw_content") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[articles] 本机抽题失败，保持待同步状态：%s: %s",
                       type(exc).__name__, exc)
        return merged
    questions = meta.get("questions") or []
    if not questions and not (meta.get("tags") or []) and not meta.get("difficulty"):
        return merged
    merged["questions"] = questions
    merged["tags"] = meta.get("tags") or []
    merged["difficulty"] = meta.get("difficulty") or ""
    merged["metadata_pending"] = False
    # 写回本地（state 保持 pending —— 它确实还没上行成功，下次同步仍要推给服务端）
    local_store.restore("articles", merged.get("id") or "", merged, state="pending")
    return merged


def delete_article(article_id: str) -> None:
    """删除面经（P4：本地先删，联网则立刻上行，离线排队）。

    语义必须保留：**不能** `except ApiError: pass` —— 旧版断网时静默成功，
    用户以为删掉了，其实服务器上还在。
    """
    old = local_store.get_item("articles", article_id)
    if old is None:
        _request("DELETE", f"/api/v1/interview/articles/{article_id}")
        _index_drop("article", article_id)
        return
    prev = {k: v for k, v in old.items() if k != "_local_state"}
    seq = local_store.soft_delete("articles", article_id)
    _index_drop("article", article_id)
    if seq is None:
        return                    # 尚未上行成功的新建 → 已彻底撤销，无需上行
    try:
        _request("DELETE", f"/api/v1/interview/articles/{article_id}")
    except ApiOfflineError:
        logger.info("[articles] 删除 %s 已存本地，等待同步", article_id)
        return
    except ApiError:
        local_store.restore("articles", article_id, prev)
        _index_doc("article", article_id, prev.get("raw_content") or "", "interview")
        raise
    local_store.resolve_op(seq, True)


def generate_interview_questions(resume_id: str, jd_id: str,
                                 article_id: Optional[str] = None,
                                 limit: int = 10) -> List[Dict[str, Any]]:
    """生成面试题（**本机生成**：本机模型 + 本地面经索引参考）。

    面经参考优先级：显式传入的 `article_id` 面经原文 > 本机索引里同类岗位面经
    （`services.interview._load_interview_reference` 里做的事）。全程不出网。

    「模型给了题但**格式全不合法**（分类/难度认不出）被丢弃」与「模型压根没给题」
    必须分开报：前者抛 `ApiError` 说明是格式问题（可重试或换模型），后者返回空列表，
    界面才会说「未生成题目，请重试」。旧版两者都是空列表，用户无从判断。
    """
    resume = get_resume(resume_id)
    jd = get_jd(jd_id)
    if not resume or not str(resume.get("raw_text") or "").strip():
        raise ApiError("简历内容为空")
    if not jd or not str(jd.get("raw_text") or "").strip():
        raise ApiError("岗位要求内容为空")

    article_content = ""
    if article_id:
        article = local_store.get_item("articles", article_id)
        if article:
            article_content = str(article.get("raw_content") or "")

    report = _interview.build_interview_questions(
        str(resume.get("raw_text") or ""), str(jd.get("raw_text") or ""),
        article_content=article_content, limit=limit or 10,
    )
    if report.get("raw_count") and not report.get("questions"):
        raise ApiError(
            f"模型返回的 {report['raw_count']} 道题分类/难度都不符合要求，已全部丢弃。"
            "可在「模型设置」里换一个更强的模型后重试。")
    return list(report.get("questions") or [])


def start_interview_simulate(resume_id: str, jd_id: str) -> Dict[str, Any]:
    """开始模拟面试（**本机会话 + 本机模型出题**），返回 `{session_id, question, question_number}`。"""
    resume = get_resume(resume_id)
    jd = get_jd(jd_id)
    if not resume:
        raise ApiError("简历不存在")
    if not jd:
        raise ApiError("JD 不存在")
    try:
        return _interview.create_session(
            resume_id, jd_id, str(resume.get("raw_text") or ""), str(jd.get("raw_text") or ""))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def submit_interview_answer(session_id: str, answer: str) -> Dict[str, Any]:
    """提交回答（本机点评 + 本机出下一题）。

    只有「会话不存在」才抛异常；点评/出题失败已在 services 层收敛成
    「这一轮照常收口」，不会让一次网络抖动把整场面试判死。
    """
    result = _interview.submit_answer(session_id, answer)
    if result.get("feedback") is None and not result.get("is_finished"):
        raise ApiError("会话不存在")
    return result


def finish_interview_simulate(session_id: str) -> None:
    """结束模拟面试（用户主动结束）。会话不存在时抛异常，不静默吞掉。"""
    if not _interview.finish_session(session_id):
        raise ApiError("会话不存在")


def get_interview_summary(session_id: str) -> Dict[str, Any]:
    """整场面试总结（本机生成）。"""
    session = _interview.get_session(session_id)
    if not session:
        raise ApiError("会话不存在")
    return _interview.summary_of(session)


# ==================== 简历生成 resume-generation ====================
# 云端前缀 /api/v1/resume-generation

def generate_resume(req: Dict[str, Any], jd_text: str = "",
                    jd_id: Optional[str] = None) -> Dict[str, Any]:
    """生成简历（云端生成并保存），返回对齐原生版 result 结构的 dict。"""
    payload = dict(req)
    payload["jd_text"] = jd_text
    if jd_id:
        payload["jd_id"] = jd_id
    data = _request("POST", "/api/v1/resume-generation/generate", data=payload)
    if not isinstance(data, dict):
        return {"error": "生成结果格式错误，请重试"}
    structured = {
        "name": data.get("name") or "",
        "position": data.get("position") or "",
        "contact": _as_dict(data.get("contact")),
        "summary": data.get("summary") or "",
        "education": _as_list(data.get("education")),
        "experiences": _as_list(data.get("experiences")),
        "skills": _as_list(data.get("skills")),
        "certifications": _as_list(data.get("certifications")),
    }
    return {
        "id": data.get("id"),
        # 网页版 `generation` 里带着这两个：生成历史用它显示「vN」，
        # 版本链用它找出「这份文档的全部版本」。桌面版以前丢掉了它们，
        # 没有版本号就没法做版本切换 —— 这里是 G2 的接口层前提。
        "version": data.get("version"),
        "document_id": data.get("document_id"),
        "structured": structured,
        "resume_text": data.get("resume_text") or "",
        "tips": _as_list(data.get("tips")),
        "ats": _as_dict(data.get("ats")),
        "gap": _as_dict(data.get("gap")),
        "fidelity": _as_dict(data.get("fidelity")),
        "review": _as_dict(data.get("quality")),
        # 生成前的输入充足度评估（score/level/message/gaps），用于提示该补哪块信息
        "sufficiency": _as_dict(data.get("sufficiency")),
    }


def regenerate_section_variants(resume_id: str, structured: Dict[str, Any],
                                section: str, index: Optional[int],
                                jd_text: str = "") -> List[Any]:
    """为指定字段生成 3 个改写候选（云端，不落库），返回候选列表。"""
    data = _request("POST", "/api/v1/resume-generation/regenerate-section-variants",
                    data={"resume_id": resume_id, "structured": structured,
                          "section": section, "index": index, "jd_text": jd_text})
    if isinstance(data, dict):
        candidates = data.get("candidates")
        return candidates if isinstance(candidates, list) else []
    return []


# ==================== 模型设置（BYOK） ====================
# 云端前缀 /api/v1/user/model-config
#
# 机制：Key 由**服务端加密**存在当前账号下，桌面版只负责提交、只读到掩码。
#   - 未配置时 using_default=True：平台用自己的默认 Key 兜底；
#   - 配置后：服务端改用你的 Key 调模型。
# 数据与 AI 依旧全部走服务器数据库，变的只是「这把 Key 是谁的」。
#
# 坑：后端 validate_base_url 会拒绝 localhost / 内网 IP（防 SSRF），
# 所以本机 Ollama（http://localhost:11434）会被服务端挡回来。

# 预设 provider 兜底表（/presets 拿不到时用），key 与云端 PRESET_PROVIDERS 对齐
FALLBACK_MODEL_PRESETS: List[Dict[str, str]] = [
    {"key": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    {"key": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"key": "moonshot", "label": "Moonshot", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"key": "qwen", "label": "通义千问", "base_url": "", "model": ""},
    {"key": "glm", "label": "智谱 GLM", "base_url": "", "model": ""},
]

PROVIDER_LABELS: Dict[str, str] = {p["key"]: p["label"] for p in FALLBACK_MODEL_PRESETS}

# API Key 最短长度（与网页版 ModelSettings.tsx 的前端校验保持一致）
MODEL_KEY_MIN_LENGTH = 8


def get_model_presets() -> List[Dict[str, str]]:
    """预设模型列表（免登录接口）。

    接口不可用时回退本地常量 —— 界面永远有可选项，不会因为一次网络抖动变空。
    """
    fallback = [dict(p) for p in FALLBACK_MODEL_PRESETS]
    try:
        data = _request("GET", "/api/v1/user/model-config/presets",
                        auth=False, timeout=15)
    except ApiError:
        return fallback
    if not isinstance(data, list):
        return fallback
    result: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        result.append({
            "key": key,
            "label": PROVIDER_LABELS.get(key) or key.capitalize(),
            "base_url": item.get("base_url") or "",
            "model": item.get("model") or "",
        })
    return result or fallback


def get_model_config() -> Dict[str, Any]:
    """当前账号的模型配置（Key 只回掩码，不回明文）。

    has_config=True：已配置自有 Key（BYOK）；
    using_default=True：未配自有 Key，但平台提供了默认模型可用。
    多回一个 `ok` 用于区分「真的没配」和「没拉到」（网络失败）。
    """
    empty: Dict[str, Any] = {
        "provider": "", "base_url": "", "model": "", "api_key_masked": "",
        "has_config": False, "using_default": False, "ok": False,
    }
    try:
        data = _request("GET", "/api/v1/user/model-config/", timeout=20)
    except ApiError:
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "provider": data.get("provider") or "",
        "base_url": data.get("base_url") or "",
        "model": data.get("model") or "",
        "api_key_masked": data.get("api_key_masked") or "",
        "has_config": bool(data.get("has_config")),
        "using_default": bool(data.get("using_default")),
        "ok": True,
    }


def describe_ai_source(cfg: Optional[Dict[str, Any]] = None) -> str:
    """AI 来源文案（状态栏 / 设置页用）。不传 cfg 则现拉一次（会走网络）。"""
    cfg = cfg if isinstance(cfg, dict) else get_model_config()
    if not cfg.get("ok"):
        return "获取失败"
    if cfg.get("has_config"):
        label = PROVIDER_LABELS.get(cfg.get("provider") or "") or cfg.get("provider") or "自有"
        return f"自有 Key（{label}）"
    if cfg.get("using_default"):
        return "平台默认模型"
    return "未配置，AI 不可用"


def test_model_connection(base_url: str, api_key: str, model: str) -> str:
    """测试模型连接。成功返回提示文案，失败抛 ApiError（消息可直接展示）。

    云端该接口失败时返回的是 HTTP 200 + `{success:false, error:...}`，
    `_request` 遇 success=false 会抛 ApiError —— 所以这里无需再判 success。
    """
    _request("POST", "/api/v1/user/model-config/test",
             data={"base_url": base_url, "api_key": api_key, "model": model},
             timeout=60)
    return "连接成功，该模型可用"


def save_model_config(provider: str, base_url: str, model: str, api_key: str) -> str:
    """保存模型配置（云端会先测连接，通过后才加密落库）。返回提示文案。"""
    data = _as_dict(_request("POST", "/api/v1/user/model-config/",
                             data={"provider": provider, "base_url": base_url,
                                   "model": model, "api_key": api_key},
                             timeout=90))
    return data.get("message") or "模型配置保存成功"


def clear_model_config() -> str:
    """清除自有 Key，回落到平台默认模型。返回提示文案。"""
    data = _as_dict(_request("DELETE", "/api/v1/user/model-config/", timeout=30))
    return data.get("message") or "模型配置已清除"


# ==================== 交流中心 community / feedback ====================
# 云端前缀 /api/v1/community 、/api/v1/feedback
# 两者列表接口都返回**分页对象** {items, total, page, page_size}，不是裸数组

POST_CATEGORIES = ["经验分享", "面经", "offer分享", "资源推荐", "其他"]
FEEDBACK_CATEGORIES = ["功能建议", "问题反馈", "体验优化", "其他"]
FEEDBACK_STATUS_LABELS = {
    "pending": "待处理",
    "accepted": "已采纳",
    "declined": "已婉拒",
    "replied": "已回复",
}


def _as_page(data: Any) -> Dict[str, Any]:
    """把云端分页对象归一成 {items: [...], total: int}（裸数组也能吃）。"""
    if isinstance(data, dict):
        items = _as_list(data.get("items"))
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = len(items)
    elif isinstance(data, list):
        items, total = data, len(data)
    else:
        items, total = [], 0
    return {"items": items, "total": total}


def _norm_post(item: Dict[str, Any]) -> Dict[str, Any]:
    """帖子字段归一（库列可为 NULL，setdefault 挡不住 None）。"""
    item["author"] = item.get("author") or "匿名用户"
    item["author_avatar"] = item.get("author_avatar") or ""
    item["title"] = item.get("title") or ""
    item["content"] = item.get("content") or ""
    item["category"] = item.get("category") or "经验分享"
    item["tags"] = _as_list(item.get("tags"))
    item["attachments"] = _as_list(item.get("attachments"))
    item["like_count"] = int(item.get("like_count") or 0)
    item["comment_count"] = int(item.get("comment_count") or 0)
    item["created_at"] = item.get("created_at") or ""
    return item


def _norm_feedback(item: Dict[str, Any]) -> Dict[str, Any]:
    """反馈字段归一。"""
    item["author"] = item.get("author") or "匿名用户"
    item["title"] = item.get("title") or ""
    item["content"] = item.get("content") or ""
    item["category"] = item.get("category") or "功能建议"
    item["status"] = item.get("status") or "pending"
    item["admin_reply"] = item.get("admin_reply") or ""
    item["like_count"] = int(item.get("like_count") or 0)
    item["created_at"] = item.get("created_at") or ""
    return item


def list_posts(category: str = "", page: int = 1,
               page_size: int = 50) -> Dict[str, Any]:
    """分享墙列表，返回 {items: [...], total}。"""
    path = f"/api/v1/community/posts?page={int(page)}&page_size={int(page_size)}"
    if category:
        path += f"&category={quote(category)}"
    try:
        data = _request("GET", path)
    except ApiError:
        return {"items": [], "total": 0}
    out = _as_page(data)
    out["items"] = [_norm_post(i) for i in out["items"] if isinstance(i, dict)]
    return out


def get_post_detail(post_id: str) -> Optional[Dict[str, Any]]:
    """帖子详情（含 comments 列表），失败返回 None。"""
    try:
        data = _request("GET", f"/api/v1/community/posts/{post_id}")
    except ApiError:
        return None
    if not isinstance(data, dict):
        return None
    item = _norm_post(data)
    comments = []
    for c in _as_list(item.get("comments")):
        if isinstance(c, dict):
            c["author"] = c.get("author") or "匿名用户"
            c["author_avatar"] = c.get("author_avatar") or ""
            c["content"] = c.get("content") or ""
            c["created_at"] = c.get("created_at") or ""
            comments.append(c)
    item["comments"] = comments
    return item


def create_post(title: str, content: str, category: str = "经验分享",
                tags: Optional[List[str]] = None,
                attachments: Optional[List[Dict[str, Any]]] = None) -> str:
    """发布分享，返回新帖 id。"""
    data = _request("POST", "/api/v1/community/posts",
                    data={"title": title, "content": content,
                          "category": category or "经验分享",
                          "tags": _as_list(tags),
                          "attachments": _as_list(attachments)})
    return (data or {}).get("id") if isinstance(data, dict) else ""


def add_post_comment(post_id: str, content: str) -> str:
    """发表评论，返回评论 id。"""
    data = _request("POST", f"/api/v1/community/posts/{post_id}/comments",
                    data={"content": content})
    return (data or {}).get("id") if isinstance(data, dict) else ""


def toggle_post_like(post_id: str) -> Dict[str, Any]:
    """点赞/取消点赞，返回 {liked, like_count}。"""
    data = _request("POST", f"/api/v1/community/posts/{post_id}/like")
    if isinstance(data, dict):
        return {"liked": bool(data.get("liked")),
                "like_count": int(data.get("like_count") or 0)}
    return {"liked": False, "like_count": 0}


def delete_post(post_id: str) -> None:
    """删除社区帖子（异常交给 UI 提示，不静默吞掉）。"""
    _request("DELETE", f"/api/v1/community/posts/{post_id}")


def list_feedbacks(category: str = "", page: int = 1,
                   page_size: int = 50) -> Dict[str, Any]:
    """建议反馈列表，返回 {items: [...], total}。"""
    path = f"/api/v1/feedback?page={int(page)}&page_size={int(page_size)}"
    if category:
        path += f"&category={quote(category)}"
    try:
        data = _request("GET", path)
    except ApiError:
        return {"items": [], "total": 0}
    out = _as_page(data)
    out["items"] = [_norm_feedback(i) for i in out["items"] if isinstance(i, dict)]
    return out


def list_my_feedbacks() -> List[Dict[str, Any]]:
    try:
        data = _request("GET", "/api/v1/feedback/mine")
    except ApiError:
        return []
    return [_norm_feedback(i) for i in _as_list(data) if isinstance(i, dict)]


def create_feedback(category: str, title: str, content: str) -> str:
    """提交建议，返回反馈 id。"""
    data = _request("POST", "/api/v1/feedback",
                    data={"category": category or "功能建议",
                          "title": title, "content": content})
    return (data or {}).get("id") if isinstance(data, dict) else ""


def toggle_feedback_like(feedback_id: str) -> Dict[str, Any]:
    data = _request("POST", f"/api/v1/feedback/{feedback_id}/like")
    if isinstance(data, dict):
        return {"liked": bool(data.get("liked")),
                "like_count": int(data.get("like_count") or 0)}
    return {"liked": False, "like_count": 0}


# ==================== 个人资料 / 账号安全 / 语料贡献 ====================
# 对齐网页版：设置页「账号与偏好」（昵称、修改密码）+「数据与隐私」（贡献语料开关）


def get_profile() -> Dict[str, Any]:
    """当前账号资料（name 是昵称、role 是角色）。失败返回空 dict。"""
    try:
        data = _request("GET", "/api/v1/auth/me")
    except ApiError:
        return {}
    d = _as_dict(data)
    d["name"] = d.get("name") or ""
    d["email"] = d.get("email") or ""
    d["role"] = (d.get("role") or "user").lower()
    # 顺手把角色写回本机登录态并落盘，供侧栏判断是否显示管理后台
    # （落盘后下次启动可直接用缓存判断，不必为了显示菜单多打一次请求）
    if isinstance(_state.get("user"), dict):
        _state["user"]["role"] = d["role"]
        if d.get("name"):
            _state["user"]["name"] = d["name"]
        _save_auth()
    return d


def is_admin_cached() -> Optional[bool]:
    """只读本机缓存的角色，**不联网**。

    返回 True/False；本地没有角色信息（旧版本登录缓存）时返回 None，
    表示「还不确定」——调用方应改为后台补查，避免主线程被网络阻塞。
    """
    u = _state.get("user") or {}
    role = str(u.get("role") or "").lower()
    if not role:
        return None
    return role == "admin"


def is_admin() -> bool:
    """当前账号是否管理员（决定侧栏是否出现「管理后台」）。

    ⚠️ 本地没有角色信息时会**同步发一次网络请求**，不要在 UI 构造路径上调用；
    菜单判定请用 is_admin_cached() + 后台补查。
    """
    cached = is_admin_cached()
    if cached is not None:
        return cached
    return get_profile().get("role") == "admin"


def get_display_name() -> str:
    """显示名：优先昵称，其次邮箱。"""
    u = _state.get("user") or {}
    return str(u.get("name") or u.get("email") or "").strip()


def update_profile(name: str) -> Dict[str, Any]:
    """修改昵称（1~32 字）。"""
    name = (name or "").strip()
    if not name:
        raise ApiError("昵称不能为空")
    if len(name) > 32:
        raise ApiError("昵称不能超过 32 个字符")
    data = _request("PATCH", "/api/v1/auth/profile", data={"name": name})
    d = _as_dict(data)
    # 同步本机登录态，界面立刻能显示新昵称
    if isinstance(_state.get("user"), dict):
        _state["user"]["name"] = d.get("name") or name
        _save_auth()
    return d


def change_password(old_password: str, new_password: str) -> str:
    """修改登录密码，返回服务端提示语。"""
    old_password = old_password or ""
    new_password = new_password or ""
    if not old_password:
        raise ApiError("请输入当前密码")
    if len(new_password) < 6:
        raise ApiError("新密码至少 6 位")
    if new_password == old_password:
        raise ApiError("新密码不能与当前密码相同")
    data = _request("POST", "/api/v1/auth/change-password",
                    data={"old_password": old_password, "new_password": new_password})
    return _as_dict(data).get("message") or "密码已修改"


def get_consent() -> bool:
    """是否同意贡献语料到共享语料库（默认关闭）。"""
    try:
        data = _request("GET", "/api/v1/corpus/consent")
    except ApiError:
        return False
    return bool(_as_dict(data).get("allow_corpus"))


def set_consent(allow: bool) -> bool:
    """开启/关闭贡献语料，返回设置后的状态。"""
    data = _request("POST", "/api/v1/corpus/consent", data={"allow": bool(allow)})
    return bool(_as_dict(data).get("allow_corpus", allow))


# ==================== 简历历史版本 ====================

def _root_document(item: Dict[str, Any], by_id: Dict[str, Dict[str, Any]],
                   memo: Dict[str, str], path: Optional[list] = None) -> str:
    """一条本地简历记录所属的「文档根」标识。

    规则（与服务端 `versioning.document_id` 同义）：
    - 写了 ``document_id`` 的直接用它（同文档的新版本都指向同一个值）；
    - 否则沿 ``parent_id`` 回溯到没有父记录的祖先，祖先的 id 就是根；
    - 都没有（导入/新建的第一版）就是它自己。

    ⚠️ 异常写入可能造出 parent 环（A→B→A），死循环会让「历史版本」Tab 直接卡住。
    检测到环时取**环上最小的 id** 当根，而不是「谁先进入谁当根」—— 后者会让
    同一环上的两个成员算出不同的根，于是它们被静默拆成两份文档，表现成
    「历史版本里少了几版」，比卡死还难查。
    """
    iid = str(item.get("id") or "")
    if iid in memo:
        return memo[iid]
    doc = str(item.get("document_id") or "")
    if doc:
        memo[iid] = doc
        return doc
    path = [] if path is None else list(path)
    if iid in path:                       # 环：环上所有成员统一取最小 id 为根
        ring = path[path.index(iid):]
        root = min(ring)
        for name in ring:
            memo[name] = root
        return root
    path.append(iid)
    pid = str(item.get("parent_id") or "")
    parent = by_id.get(pid) if pid else None
    root = _root_document(parent, by_id, memo, path) if parent is not None else iid
    memo[iid] = root
    return root


def _local_chain(resume_id: str) -> List[Dict[str, Any]]:
    """本地主存里与 ``resume_id`` 同属一份文档的全部记录（含它自己）。

    本地主存通常只有「每个文档的最新一版」（下行 `list_resumes` 只镜像最新），
    所以这不是「全部历史」，只是**云端还不知道的那部分**。返回空列表表示
    「本地没有这条记录」，调用方据此决定是否回落云端。
    """
    items = [it for it in local_store.list_items("resumes") if isinstance(it, dict)]
    by_id = {str(it.get("id") or ""): it for it in items}
    target = by_id.get(str(resume_id))
    if target is None:
        return []
    memo: Dict[str, str] = {}
    root = _root_document(target, by_id, memo)
    return [it for it in items
            if str(it.get("id") or "") and _root_document(it, by_id, memo) == root]


def list_resume_versions(resume_id: str) -> List[Dict[str, Any]]:
    """某份简历的全部历史版本（**云端 ∪ 本地**，按版本号倒序）。

    为什么必须并上本地那一份：本机新建 / 生成 / 优化出来的新版本在**上行成功之前**
    云端根本不知道，`GET /{id}/versions` 直接 404 —— 只读云端的话，用户刚点完
    「优化」再切到「历史版本」，看到的是一片空白，像是没保存。而只读本地也不行：
    本地主存里通常只有「每个文档的最新一版」，老版本只存在于云端。所以取并集。
    """
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()
    try:
        data = _request("GET", f"/api/v1/resume/{resume_id}/versions")
    except ApiError:
        # 离线 / 未上行（404）/ 掉登录：都不是错误，静默降级到本地链
        data = None
    for v in _as_list(data):
        if not isinstance(v, dict):
            continue
        v["version"] = int(v.get("version") or 0)
        v["raw_text"] = v.get("raw_text") or ""
        v["source_file"] = v.get("source_file") or ""
        v["created_at"] = v.get("created_at") or ""
        seen_ids.add(str(v.get("id") or ""))
        out.append(v)

    for it in _local_chain(resume_id):
        iid = str(it.get("id") or "")
        if iid in seen_ids:
            continue
        out.append({
            "id": iid,
            "version": int(it.get("version") or 0),
            "raw_text": it.get("raw_text") or "",
            "source_file": it.get("source_file") or "",
            "parsed_json": it.get("parsed_json"),
            "created_at": it.get("created_at") or "",
            "_local_only": True,
        })
    out.sort(key=lambda x: x["version"], reverse=True)
    return out


def rollback_resume_version(resume_id: str, version_id: str) -> Dict[str, Any]:
    """把简历回滚到指定历史版本（版本链两端都在本地时**不联网**）。

    语义与服务端 `rollback_to_version` 一致：**不是**把旧记录改回「当前版本」，
    而是复制目标版本内容**追加一条新的最高版本**，并在 ``parsed_json`` 里记
    ``rolled_back_from`` —— 历史一个都不少。跨文档回滚直接拒绝（同样与服务端一致）。
    """
    if not resume_id or not version_id:
        raise ApiError("缺少简历或版本标识")

    items = [it for it in local_store.list_items("resumes") if isinstance(it, dict)]
    by_id = {str(it.get("id") or ""): it for it in items}
    source, target = by_id.get(str(resume_id)), by_id.get(str(version_id))
    if source is not None and target is not None:
        memo: Dict[str, str] = {}
        if _root_document(source, by_id, memo) != _root_document(target, by_id, memo):
            raise ApiError("所选版本不属于同一份简历")
        chain = [it for it in items
                 if str(it.get("id") or "") and _root_document(it, by_id, memo)
                 == _root_document(source, by_id, memo)]
        latest = max(int(it.get("version") or 0) for it in chain)
        item_id = uuid.uuid4().hex
        parsed = dict(target.get("parsed_json") or {})
        parsed["rolled_back_from"] = int(target.get("version") or 0)
        item: Dict[str, Any] = {
            "id": item_id,
            "version": latest + 1,
            "raw_text": target.get("raw_text") or "",
            "source_file": target.get("source_file") or "",
            "source": target.get("source") or "rollback",
            "parsed_json": parsed,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "parent_id": str(target.get("id") or "") or None,
            # 沿用一个稳定的文档根，保证这次回滚出来的新版本仍属于同一份简历
            "document_id": source.get("document_id") or str(source.get("id") or ""),
        }
        seq = local_store.save("resumes", "create", item_id, item)
        if seq is not None:
            return {"id": item_id, "version": item["version"]}
        # 未登录 / 本地不可写：本地主存本来就按账号隔离，落不下去就交回云端
    data = _request(
        "POST", f"/api/v1/resume/{resume_id}/versions/{version_id}/rollback"
    )
    return _as_dict(data)


# ==================== 投递提醒（后端规则） ====================

def list_reminders() -> Dict[str, Any]:
    """后端算出的待跟进提醒：超期未跟进（overdue）+ 即将到期（upcoming）。"""
    try:
        data = _request("GET", "/api/v1/applications/reminders")
    except ApiError:
        data = None
    d = _as_dict(data)

    def _items(key: str) -> List[Dict[str, Any]]:
        out = []
        for it in _as_list(d.get(key)):
            if not isinstance(it, dict):
                continue
            it["company"] = it.get("company") or "（未填公司）"
            it["position"] = it.get("position") or "（未填岗位）"
            try:
                it["days_since_update"] = int(it.get("days_since_update") or 0)
            except (TypeError, ValueError):
                it["days_since_update"] = 0
            out.append(it)
        return out

    overdue, upcoming = _items("overdue"), _items("upcoming")
    return {
        "overdue": overdue,
        "upcoming": upcoming,
        "overdue_count": int(d.get("overdue_count") or len(overdue)),
        "upcoming_count": int(d.get("upcoming_count") or len(upcoming)),
        "has_reminders": bool(d.get("has_reminders") or overdue or upcoming),
    }


# ==================== AI 用量与成本（observability） ====================

def _empty_cost_summary() -> Dict[str, Any]:
    return {"total_cost": 0.0, "total_cost_cny": 0.0, "total_tokens": 0, "trace_count": 0,
            "by_operation": [], "by_model": []}


def get_cost_summary() -> Dict[str, Any]:
    """当前账号的 AI 成本汇总：总费用/token/调用次数 + 按操作、按模型分布。

    后端同时给出美元原值（`*_cost`，对齐模型厂商账单）与人民币折算值（`*_cny`）。
    界面展示统一用人民币，故下游一律读 `_cny` 字段。
    """
    try:
        data = _request("GET", "/api/v1/observability/summary")
    except ApiError:
        return _empty_cost_summary()
    d = _as_dict(data)

    def _rows(key: str) -> List[Dict[str, Any]]:
        rows = []
        for it in _as_list(d.get(key)):
            if not isinstance(it, dict):
                continue
            rows.append({
                "key": it.get("operation") or it.get("model") or "-",
                "cost": float(it.get("cost") or 0),
                "cost_cny": float(it.get("cost_cny") or 0),
                "tokens": int(it.get("tokens") or 0),
                "count": int(it.get("count") or 0),
            })
        rows.sort(key=lambda x: x["cost"], reverse=True)
        return rows

    out = _empty_cost_summary()
    out["total_cost"] = float(d.get("total_cost") or 0)
    out["total_cost_cny"] = float(d.get("total_cost_cny") or 0)
    out["total_tokens"] = int(d.get("total_tokens") or 0)
    out["trace_count"] = int(d.get("trace_count") or 0)
    out["by_operation"] = _rows("by_operation")
    out["by_model"] = _rows("by_model")
    return out


def list_cost_traces(page: int = 1, page_size: int = 20,
                     operation: Optional[str] = None) -> Dict[str, Any]:
    """AI 调用明细（注意：返回的是**裸数组**，不是分页对象）。"""
    q = {"page": max(1, int(page)), "page_size": max(1, min(100, int(page_size)))}
    if operation:
        q["operation"] = operation
    try:
        data = _request("GET", "/api/v1/observability/traces?" + urlencode(q))
    except ApiError:
        return {"items": [], "total": 0}
    items = []
    for it in _as_list(data):
        if not isinstance(it, dict):
            continue
        it["operation"] = it.get("operation") or "-"
        it["status"] = it.get("status") or "success"
        it["total_tokens"] = int(it.get("total_tokens") or 0)
        it["total_cost"] = float(it.get("total_cost") or 0)
        it["total_cost_cny"] = float(it.get("total_cost_cny") or 0)
        it["duration_ms"] = int(it.get("duration_ms") or 0)
        items.append(it)
    return {"items": items, "total": len(items)}


def get_trace_detail(trace_id: str) -> Dict[str, Any]:
    """单次调用的链路明细：``{"trace": {...}, "spans": [...]}``。

    `spans` 由服务端按 `created_at.asc()` 排好序（就是时间顺序），下游不要再排。
    `cost_cny` **可能是 None**（模型价格表里查不到该模型时后端给 null），
    这里保留 None 原样传下去 —— 折成 0 会显示成「这次调用不花钱」，是错的。
    """
    data = _request("GET", f"/api/v1/observability/traces/{trace_id}")
    if not isinstance(data, dict):
        return {"trace": {}, "spans": []}
    trace = _as_dict(data.get("trace"))
    spans = []
    for it in _as_list(data.get("spans")):
        if not isinstance(it, dict):
            continue
        it["model"] = it.get("model") or ""
        it["prompt_tokens"] = int(it.get("prompt_tokens") or 0)
        it["completion_tokens"] = int(it.get("completion_tokens") or 0)
        it["total_tokens"] = int(it.get("total_tokens") or 0)
        it["duration_ms"] = int(it.get("duration_ms") or 0)
        it["start_offset_ms"] = int(it.get("start_offset_ms") or 0)
        it["status"] = it.get("status") or "success"
        if it.get("cost_cny") is not None:
            it["cost_cny"] = float(it["cost_cny"])
        spans.append(it)
    return {"trace": trace, "spans": spans}


# ==================== 社区评论删除 / 反馈管理 ====================

def delete_post_comment(comment_id: str) -> None:
    """删除一条评论（作者本人或管理员）。"""
    _request("DELETE", f"/api/v1/community/comments/{comment_id}")


def admin_delete_post(post_id: str) -> None:
    """管理员删除帖子 —— 失败要抛出（社区页那个静默版不适合管理操作）。"""
    _request("DELETE", f"/api/v1/community/posts/{post_id}")


def admin_update_feedback(feedback_id: str, status: Optional[str] = None,
                          admin_reply: Optional[str] = None) -> Dict[str, Any]:
    """管理员处理建议反馈：改状态 / 写官方回复。"""
    body: Dict[str, Any] = {}
    if status:
        body["status"] = status
    if admin_reply is not None:
        body["admin_reply"] = admin_reply
    if not body:
        raise ApiError("没有需要更新的内容")
    data = _request("PATCH", f"/api/v1/feedback/{feedback_id}", data=body)
    return _as_dict(data)


# ==================== 管理后台 admin ====================
# 全部要求 role=admin（服务端 require_role 把关，403/404 会被 _request 抛成 ApiError）

ROLE_LABELS = {"admin": "管理员", "user": "普通用户"}


def _admin_fail(exc: Exception, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """管理接口读取失败时的兜底：保留结构 + 把原因放进 `_error`。

    管理后台是**需要权限**的页面，静默返回空数据会让用户看到「空白但没有任何提示」，
    分不清是「确实没数据」还是「没权限 / 网络断了」。UI 层据此显示明确提示。
    """
    out = dict(default or {})
    out["_error"] = str(exc)
    return out


def admin_overview(days: int = 30) -> Dict[str, Any]:
    """管理总览：用户/用量/内容规模 + 每日趋势。"""
    days = max(7, min(90, int(days or 30)))
    try:
        data = _request("GET", f"/api/v1/admin/overview?days={days}")
    except ApiError as exc:
        return _admin_fail(exc, {"users": {}, "usage": {}, "content": {}, "trend": []})
    d = _as_dict(data)
    trend = []
    for it in _as_list(d.get("trend")):
        if not isinstance(it, dict):
            continue
        trend.append({
            "date": str(it.get("date") or ""),
            "calls": int(it.get("calls") or 0),
            "cost": float(it.get("cost") or 0),
            "cost_cny": float(it.get("cost_cny") or 0),
            "tokens": int(it.get("tokens") or 0),
        })
    d["trend"] = trend
    d["users"] = _as_dict(d.get("users"))
    d["usage"] = _as_dict(d.get("usage"))
    d["content"] = _as_dict(d.get("content"))
    return d


def admin_list_users(q: Optional[str] = None, role: Optional[str] = None,
                     is_active: Optional[bool] = None, byok: Optional[bool] = None,
                     page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    """用户列表（分页对象）。"""
    query: Dict[str, Any] = {"page": max(1, int(page)),
                             "page_size": max(1, min(100, int(page_size)))}
    if q:
        query["q"] = q
    if role:
        query["role"] = role
    if is_active is not None:
        query["is_active"] = str(bool(is_active)).lower()
    if byok is not None:
        query["byok"] = str(bool(byok)).lower()
    try:
        data = _request("GET", "/api/v1/admin/users?" + urlencode(query))
    except ApiError as exc:
        return _admin_fail(exc, {"items": [], "total": 0})
    out = _as_page(data)
    clean = []
    for it in out["items"]:
        if not isinstance(it, dict):
            continue
        it["name"] = it.get("name") or "（未设昵称）"
        it["email"] = it.get("email") or ""
        it["role"] = (it.get("role") or "user").lower()
        it["usage"] = _as_dict(it.get("usage"))
        clean.append(it)
    out["items"] = clean
    return out


def admin_user_detail(user_id: str) -> Dict[str, Any]:
    """单个用户的详情（含各业务表条数与最近调用）。"""
    try:
        data = _request("GET", f"/api/v1/admin/users/{user_id}/detail")
    except ApiError as exc:
        return _admin_fail(exc)
    return _as_dict(data)


def admin_update_user_role(user_id: str, role: str) -> Dict[str, Any]:
    """改角色（admin / user）。"""
    role = (role or "").lower()
    if role not in ("admin", "user"):
        raise ApiError("角色只能是 admin 或 user")
    return _as_dict(_request("PUT", f"/api/v1/admin/users/{user_id}/role",
                             data={"role": role}))


def admin_update_user_active(user_id: str, is_active: bool) -> Dict[str, Any]:
    """启用 / 停用账号。"""
    return _as_dict(_request("PUT", f"/api/v1/admin/users/{user_id}/active",
                             data={"is_active": bool(is_active)}))


def admin_reset_user_password(user_id: str, new_password: str) -> str:
    """管理员直接重置某用户密码。"""
    if len(new_password or "") < 6:
        raise ApiError("新密码至少 6 位")
    data = _request("POST", f"/api/v1/admin/users/{user_id}/reset_password",
                    data={"new_password": new_password})
    return _as_dict(data).get("message") or "密码已重置"


def admin_bind_user_email(user_id: str, email: str) -> str:
    """管理员直接改绑某用户邮箱（不需要验证码）。"""
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ApiError("请输入正确的邮箱")
    data = _request("POST", f"/api/v1/admin/users/{user_id}/bind_email",
                    data={"email": email})
    return _as_dict(data).get("message") or "邮箱已绑定"


def admin_usage(dimension: str = "operation", days: Optional[int] = None,
                limit: int = 50) -> Dict[str, Any]:
    """平台用量聚合，dimension: user | operation | model | day。"""
    if dimension not in ("user", "operation", "model", "day"):
        dimension = "operation"
    query: Dict[str, Any] = {"dimension": dimension,
                             "limit": max(1, min(200, int(limit)))}
    if days:
        query["days"] = max(1, min(365, int(days)))
    try:
        data = _request("GET", "/api/v1/admin/usage?" + urlencode(query))
    except ApiError as exc:
        return _admin_fail(exc, {"dimension": dimension, "items": [], "total": {}})
    d = _as_dict(data)
    items = []
    for it in _as_list(d.get("items")):
        if not isinstance(it, dict):
            continue
        items.append({
            "key": str(it.get("key") or "-"),
            "calls": int(it.get("calls") or 0),
            "tokens": int(it.get("tokens") or 0),
            "cost": float(it.get("cost") or 0),
            "cost_cny": float(it.get("cost_cny") or 0),
            "avg_duration_ms": int(it.get("avg_duration_ms") or 0),
            "failed": int(it.get("failed") or 0),
        })
    d["items"] = items
    d["total"] = _as_dict(d.get("total"))
    return d


def admin_usage_traces(page: int = 1, page_size: int = 20,
                       user_id: Optional[str] = None,
                       operation: Optional[str] = None,
                       trace_status: Optional[str] = None) -> Dict[str, Any]:
    """平台全部调用明细。"""
    query: Dict[str, Any] = {"page": max(1, int(page)),
                             "page_size": max(1, min(100, int(page_size)))}
    if user_id:
        query["user_id"] = user_id
    if operation:
        query["operation"] = operation
    if trace_status:
        query["status"] = trace_status
    try:
        data = _request("GET", "/api/v1/admin/usage/traces?" + urlencode(query))
    except ApiError as exc:
        return _admin_fail(exc, {"items": [], "total": 0})
    d = _as_dict(data)
    return {"items": _as_list(d.get("items")), "total": int(d.get("total") or 0)}


def admin_announcement() -> Dict[str, Any]:
    """平台公告状态与内容。"""
    try:
        data = _request("GET", "/api/v1/admin/announcement")
    except ApiError as exc:
        return _admin_fail(exc, {"enabled": False, "content": ""})
    d = _as_dict(data)
    return {"enabled": bool(d.get("enabled")), "content": d.get("content") or ""}


def admin_health() -> Dict[str, Any]:
    """系统健康：数据库、默认模型 Key、调用统计、失败与最慢调用。"""
    try:
        data = _request("GET", "/api/v1/admin/health")
    except ApiError as exc:
        return _admin_fail(exc)
    d = _as_dict(data)
    d["database"] = _as_dict(d.get("database"))
    d["default_key"] = _as_dict(d.get("default_key"))
    d["traces"] = _as_dict(d.get("traces"))
    d["recent_failures"] = _as_list(d.get("recent_failures"))
    d["slowest"] = _as_list(d.get("slowest"))
    return d


def admin_audit_logs(page: int = 1, page_size: int = 20,
                     action: Optional[str] = None) -> Dict[str, Any]:
    """管理员操作审计日志（分页）。"""
    query: Dict[str, Any] = {"page": max(1, int(page)),
                             "page_size": max(1, min(100, int(page_size)))}
    if action:
        query["action"] = action
    try:
        data = _request("GET", "/api/v1/admin/audit-logs?" + urlencode(query))
    except ApiError as exc:
        return _admin_fail(exc, {"items": [], "total": 0, "actions": []})
    d = _as_dict(data)
    out = _as_page(d)
    out["actions"] = [str(a) for a in _as_list(d.get("actions"))]
    return out


def admin_settings() -> Dict[str, Any]:
    """平台设置项 + 平台默认模型信息。"""
    try:
        data = _request("GET", "/api/v1/admin/settings")
    except ApiError as exc:
        return _admin_fail(exc, {"settings": {}, "platform_model": {}})
    d = _as_dict(data)
    raw = _as_dict(d.get("settings"))
    settings = {}
    for key, val in raw.items():
        v = _as_dict(val)
        v["key"] = v.get("key") or key
        v["description"] = v.get("description") or ""
        settings[key] = v
    d["settings"] = settings
    d["platform_model"] = _as_dict(d.get("platform_model"))
    return d


def admin_update_setting(key: str, value: Any) -> Dict[str, Any]:
    """改一个平台设置项。"""
    if not key:
        raise ApiError("缺少设置项名称")
    return _as_dict(_request("PUT", f"/api/v1/admin/settings/{key}",
                             data={"value": value}))


def admin_export_users(dest_path: str) -> str:
    """导出全部用户为 CSV（服务端返回文件流，不走 _request）。"""
    url = get_base_url() + "/api/v1/admin/users/export"
    headers = {}
    if _state["token"]:
        headers["Authorization"] = f"Bearer {_state['token']}"
    try:
        resp = requests.get(url, headers=headers, timeout=120)
    except requests.exceptions.Timeout:
        raise ApiError("导出超时，请稍后重试")
    except requests.exceptions.ConnectionError:
        raise ApiError("无法连接服务器，请检查网络")
    if resp.status_code == 401:
        clear_auth()
        raise ApiError("登录已失效，请重新登录")
    if resp.status_code >= 400:
        raise ApiError(f"导出失败（HTTP {resp.status_code}）")
    with open(dest_path, "wb") as fh:
        fh.write(resp.content)
    return dest_path


# ==================== 求职 Agent（v0.2-beta） ====================
# 对齐网页版 AgentConsole.tsx / openCareerAgent.ts。
# 协议要点（来自 backend/app/modules/career_agent/routes.py，权威）：
#   - 所有端点前缀 /api/v1/agent。
#   - 信封统一 {success, data, error}；_request 已解包，本组方法直接返回 data。
#   - 创建会话的请求体只能是 {entry_source, jd_id, resume_id, application_id}，
#     **不接受 context 字段**（与网页版 openCareerAgent.ts 一致）。
#   - session_id == thread_id；同一会话同一时刻只能有一条消息在跑（409 锁）。
#   - 写业务数据的操作必须用户确认：POST .../actions/{action_id} 体 {approved:bool}。
#   - 确认/拒绝失败按 HTTP 状态码分支（410 过期、409 已处理/冲突、404 找不到），
#     页面层**不**匹配服务端中文文案。
#   - pending_action / write_failure 里的状态与失败码一律读服务端原值，本地不重算、不猜。

AGENT_BASE = "/api/v1/agent"


def agent_create_session(*, entry_source: str = "unknown",
                         jd_id: Optional[str] = None,
                         resume_id: Optional[str] = None,
                         application_id: Optional[str] = None) -> Dict[str, Any]:
    """创建（或恢复）Agent 会话。同上下文复用会返回 resumed=True。

    请求体只带四个字段，缺省传 None（后端按"未选择"处理，不报错）。
    """
    data = _request("POST", f"{AGENT_BASE}/sessions",
                    data={"entry_source": entry_source or "unknown",
                          "jd_id": jd_id, "resume_id": resume_id,
                          "application_id": application_id})
    return _as_dict(data)


def agent_get_session(session_id: str) -> Dict[str, Any]:
    """读取会话状态（恢复页面/刷新后重拉都靠它）。他人会话后端返回 404→ApiError。"""
    data = _request("GET", f"{AGENT_BASE}/sessions/{session_id}")
    return _as_dict(data)


def agent_run_message(session_id: str, message: str) -> Dict[str, Any]:
    """运行一条用户消息，返回 RunView。会话被锁（另一条在跑）会 409→ApiError。"""
    data = _request("POST", f"{AGENT_BASE}/sessions/{session_id}/messages",
                    data={"message": message})
    return _as_dict(data)


def agent_get_trace(session_id: str) -> Dict[str, Any]:
    """读取会话的运行轨迹 TraceView（runs: [...]）。"""
    data = _request("GET", f"{AGENT_BASE}/sessions/{session_id}/trace")
    return _as_dict(data)


def agent_list_tools() -> List[Dict[str, Any]]:
    """列出 Agent 可用工具 List[ToolView]。"""
    try:
        data = _request("GET", f"{AGENT_BASE}/tools")
    except ApiError:
        return []
    return _as_list(data)


def agent_list_actions(session_id: str) -> List[Dict[str, Any]]:
    """列出该会话待处理的写操作 List[ActionIntentView]。

    不吞错：页面层有专属错误处理（_on_pending_error），需要把失败透传过去，
    而不是静默返回空列表（否则「服务端在等你确认、页面却什么都没有」）。
    """
    data = _request("GET", f"{AGENT_BASE}/sessions/{session_id}/actions")
    return _as_list(data)


def agent_decide_action(session_id: str, action_id: str, approved: bool) -> Dict[str, Any]:
    """确认或拒绝一个待执行写操作，返回 RunView。

    410=已过期、409=已处理/冲突、404=找不到：页面层按状态码分支，**不**匹配中文文案。
    """
    data = _request("POST",
                    f"{AGENT_BASE}/sessions/{session_id}/actions/{action_id}",
                    data={"approved": bool(approved)})
    return _as_dict(data)


def agent_list_memories(status_filter: Optional[str] = None,
                        memory_type: Optional[str] = None,
                        limit: int = 100) -> List[Dict[str, Any]]:
    """列出我的长期记忆 List[MemoryView]（proposed/confirmed 等由页面分区）。

    不吞错：页面层有专属错误处理（_on_memories_error）。
    """
    path = f"{AGENT_BASE}/memories?limit={int(limit)}"
    if status_filter:
        path += f"&status_filter={quote(status_filter)}"
    if memory_type:
        path += f"&memory_type={quote(memory_type)}"
    data = _request("GET", path)
    return _as_list(data)


def agent_create_memory(content: str, conflict_key: Optional[str] = None) -> Dict[str, Any]:
    """显式添加一条偏好（你说的话直接生效，不需要再确认）。

    content 限 500 字、最多 3 行；长文与凭据类内容会被服务端 400 拒绝→ApiError。
    """
    data = _request("POST", f"{AGENT_BASE}/memories",
                    data={"content": content, "conflict_key": conflict_key})
    return _as_dict(data)


def agent_confirm_memory(memory_id: str) -> Dict[str, Any]:
    """确认一条待确认记忆（proposed → confirmed）。404/409→ApiError。"""
    data = _request("POST", f"{AGENT_BASE}/memories/{memory_id}/confirm")
    return _as_dict(data)


def agent_delete_memory(memory_id: str) -> Dict[str, Any]:
    """删除一条记忆（同步删结构化记录与向量索引），返回 {memory_id, deleted}。"""
    data = _request("DELETE", f"{AGENT_BASE}/memories/{memory_id}")
    return _as_dict(data)
