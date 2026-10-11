"""云端参考库：调用网页版语料库的 RAG 检索接口（HTTP 只读、免登录）。

⚠️ P3 起**不再是默认检索路径**。
    P2 已把服务器语料全量拉到本地并重算向量（`local_ai/index.db`，向量与服务器
    逐位一致，已交叉验证），因此桌面版的「生成前查库」默认走
    `local_ai.retrieve`（本地、离线可用、带 0.55 相似度阈值）。
    本模块降级为**可选兜底**：仅在 `retrieve_context(fallback_cloud=True)`
    时被调用，用于「服务器语料已更新但本地尚未重建索引」的窗口期。

    实测发现把它当默认兜底有害：服务器 `/corpus/search` 无相似度阈值，
    本地已正确滤掉的无关内容（如"红烧肉的做法"）会被它重新塞回 prompt。
    故 `local_ai.retrieve` 的 `fallback_cloud` 默认为 False。

    接口只读、需登录（自 2026-10 服务端加固起，`/corpus/search` 与其它语料接口
    一致，要求带 Bearer Token；匿名会拿到 401）。离线/超时/失败一律静默降级为空串，
    不影响主流程。
"""
from __future__ import annotations

from typing import List, Optional

import requests

# 兜底地址（settings 表里的 `cloud.base_url` 优先）。
# 默认指向本机后端，便于本地起 `docker compose up` 后直接联调。
DEFAULT_CLOUD_URL = "http://localhost:8001"


def get_cloud_url() -> str:
    """云端地址（默认指向已部署的网页版）。"""
    try:
        from .. import database as db
        conn = db.get_conn()
        row = conn.execute("SELECT value FROM settings WHERE key='cloud.base_url'").fetchone()
        if row and (row["value"] or "").strip():
            return row["value"].strip().rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_CLOUD_URL


def set_cloud_url(url: str) -> None:
    from .. import database as db
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('cloud.base_url', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ((url or "").strip().rstrip("/"),),
    )
    conn.commit()


def _headers() -> dict:
    """带登录态：`/corpus/search` 需要 Bearer Token，未登录必然 401。

    复用 api_client 已保存的登录态（与 `local_ai/pull.py` 同一套做法）；
    没有 token 时返回空头，服务端会 401，由上层静默降级。
    """
    try:
        from .. import api_client
        token = api_client.get_token()
    except Exception:  # noqa: BLE001
        token = None
    return {"Authorization": f"Bearer {token}"} if token else {}


def _search(query: str, item_type: Optional[str], top_k: int, timeout: float) -> List[str]:
    """调用云端 /api/v1/corpus/search，返回命中的 raw_text 列表（只读、需登录）。"""
    url = get_cloud_url() + "/api/v1/corpus/search"
    payload = {"query_text": query, "top_k": top_k}
    if item_type:
        payload["item_type"] = item_type
    resp = requests.post(url, json=payload, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("data") or []) if isinstance(data, dict) else []
    texts: List[str] = []
    for r in results:
        if isinstance(r, dict) and (r.get("raw_text") or "").strip():
            texts.append(r["raw_text"].strip())
    return texts


def retrieve_reference(query: str, item_type: str = "resume", top_k: int = 3,
                       max_chars: int = 1200, timeout: float = 8.0) -> str:
    """检索云端语料，拼成参考文本；任何异常返回空串（静默降级）。"""
    if not (query or "").strip():
        return ""
    try:
        texts = _search(query, item_type, top_k, timeout)
        if not texts:
            return ""
        return "\n\n".join(t[:max_chars] for t in texts)
    except Exception:  # noqa: BLE001
        return ""
