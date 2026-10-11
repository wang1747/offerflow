"""本地索引「新鲜度」检测：判断本地快照 / 索引是否已经落后于服务器。

为什么需要（计划 §9.5-1）：
    P2 的索引是「一次性拉全量 → 本地重算」的快照。**服务器语料更新后，本地不会
    自动跟随**，界面上也没有任何提示 —— 用户会一直用过期语料生成的参考内容，
    且完全无感知。本模块把这件事变成可见、可操作的一件事。

探针选择：`GET /api/v1/corpus/stats`（需登录）
    返回形如 `{"resume": 144, "jd": 55, "interview": 157}`，**只有计数、不传全文**，
    是现成的轻量探针（P0 新增的 `/corpus/export` 太重：356 条全文约 140KB）。

**不做自动轮询**：只有用户点「检查更新」（或显式 `check(remote=True)`）才联网，
与「离线优先」的目标不冲突 —— 离线时本模块只回答本地那一半。

判定为「建议重建」的几种情况：
    1. 本地还没有索引 / 没有语料快照；
    2. 服务器语料条数与本地快照不一致（语料变了）；
    3. 快照比索引新（拉过数据但没重建索引）；
    4. 当前索引是 `hash_fallback` 降级产物，而现在模型已经可用（降级索引精度差）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CORPUS_STATS_PATH = "/api/v1/corpus/stats"


def local_state() -> Dict[str, Any]:
    """本地现状（**不联网**）：索引 + 四路快照 + 后端/模型。"""
    from . import index as index_mod

    st = index_mod.status()
    idx = st.get("index") or {}
    sources = st.get("sources") or []
    by_src = {s.get("source"): s for s in sources if isinstance(s, dict)}
    corpus = by_src.get("corpus") or {}
    return {
        "index": {
            "exists": bool(idx.get("exists")),
            "chunks": idx.get("chunks", 0),
            "items": idx.get("items", 0),
            "built_at": idx.get("built_at"),
            "backend": idx.get("backend"),
            "model": idx.get("model"),
        },
        "corpus_snapshot": {
            "count": corpus.get("count", 0),
            "fetched_at": corpus.get("fetched_at"),
        },
        "sources": sources,
        "index_db": st.get("index_db"),
        "embedder": st.get("embedder") or {},
    }


def remote_state(timeout: int = 15) -> Dict[str, Any]:
    """轻量探针：服务器语料各类型条数。

    失败一律不抛异常，返回 `{"ok": False, "error": ...}` —— 调用方（UI）
    需要能在离线时优雅地只显示本地那一半。
    """
    import requests

    from .. import api_client

    headers = {"Content-Type": "application/json"}
    token = api_client.get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = api_client.get_base_url() + CORPUS_STATS_PATH
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("语料探针失败：%s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code == 401:
        return {"ok": False, "error": "登录已失效，请重新登录"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"HTTP {resp.status_code}"}
    try:
        body = resp.json()
    except ValueError:
        return {"ok": False, "error": "返回不是合法 JSON"}

    data = body.get("data") if isinstance(body, dict) and "success" in body else body
    if not isinstance(data, dict):
        return {"ok": False, "error": "语料统计返回结构异常"}
    try:
        by_type = {str(k): int(v) for k, v in data.items()}
    except (TypeError, ValueError):
        return {"ok": False, "error": "语料统计内容异常"}
    return {"ok": True, "by_type": by_type, "total": sum(by_type.values())}


def check(timeout: int = 15, remote: bool = True) -> Dict[str, Any]:
    """综合判定是否需要重建索引。

    返回：
        {
          "stale":    bool,          # True = 建议重建
          "reasons":  [...],         # 人话理由（给界面直接显示）
          "local":    {...},         # local_state()
          "remote":   {...},         # remote_state()；remote=False 或失败时为 None/ok=False
        }
    """
    local = local_state()
    idx = local["index"]
    snap = local["corpus_snapshot"]
    reasons: List[str] = []

    if not idx["exists"]:
        reasons.append("本地还没有建立索引")
    else:
        if not snap.get("count"):
            reasons.append("本地还没有语料快照")
        if idx.get("backend") == "hash_fallback" and local.get("embedder", {}).get("available"):
            reasons.append("当前索引是词袋降级产物，而本地模型已可用（精度偏低，建议重建）")
        # 模型不一致 → 向量空间不同，旧索引直接作废（换模型必须重建）
        cur_model = (local.get("embedder") or {}).get("model") or ""
        idx_model = idx.get("model") or ""
        if cur_model:
            if idx_model and idx_model != cur_model:
                reasons.append(f"索引由旧模型 {idx_model} 建立，当前模型为 {cur_model}（必须重建）")
            elif not idx_model:
                reasons.append("索引未记录所用模型，无法校验是否与当前模型一致（建议重建）")
        # 快照比索引新 → 拉过数据但没重建
        b, f = idx.get("built_at"), snap.get("fetched_at")
        if b and f and str(f) > str(b):
            reasons.append("语料快照比索引新（拉取过数据但未重建索引）")

    remote_info: Optional[Dict[str, Any]] = None
    if remote:
        remote_info = remote_state(timeout=timeout)
        if remote_info.get("ok") and snap.get("count"):
            if int(remote_info["total"]) != int(snap["count"]):
                reasons.append(
                    f"服务器语料 {remote_info['total']} 条，本地快照 {snap['count']} 条（已变化）"
                )
        elif not remote_info.get("ok"):
            logger.info("未取到服务器语料状态：%s", remote_info.get("error"))

    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "local": local,
        "remote": remote_info,
    }


__all__ = ["CORPUS_STATS_PATH", "local_state", "remote_state", "check"]
