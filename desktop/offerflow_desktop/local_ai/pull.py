"""P2 四路全量拉取：把服务器上的权威数据拉到本地快照，供本地重算索引。

四路（与计划 §3 一致）：

    corpus    GET /api/v1/corpus/export                       共享语料（含 raw_text 全文）
    resumes   GET /api/v1/resume/export-data                  当前用户全部简历（含全文）
    jd        GET /api/v1/jd/list                             当前用户岗位（含 raw_text 全文）
    salary    GET /api/v1/salary-negotiation/benchmark/data   生效薪资基准系数（**不进向量库**）

为什么 corpus 必须用 /export 而不是 /list：
    /list 只回 raw_text_preview（前 200 字）且 limit 默认 50，
    结构上无法用于本地重算（P0 已为桌面版新增 /export 只读端点）。

为什么 salary 也要拉：
    桌面版内置的是出厂基线（= 服务器 v1），而服务器生效的可能是更新版本（实测 v2
    学历基数普遍更低）。不拉会让本地算出的薪资系统性偏高。它是**规则表**，
    走确定性规则引擎，不进向量库、不参与语义检索。

鉴权：复用 api_client 已保存的登录态（`data/auth.json` 的 token）。
快照落地：`local_ai/cache/{corpus,resumes,jd,salary}.json`，结构统一为
    {"source": ..., "fetched_at": ISO, "count": n, "items": <原始数据>}
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from .paths import CACHE_DIR, cache_path

# 每路：(展示名, 路径, 超时秒)
_ROUTES: Dict[str, tuple] = {
    "corpus": ("共享语料库", "/api/v1/corpus/export", 60),
    "resumes": ("我的简历", "/api/v1/resume/export-data", 30),
    "jd": ("我的岗位 JD", "/api/v1/jd/list", 30),
    "salary": ("薪资基准（规则表）", "/api/v1/salary-negotiation/benchmark/data", 30),
}

SOURCES = tuple(_ROUTES.keys())


class PullError(RuntimeError):
    """拉取失败（未登录 / 网络不可达 / 服务器返回异常）。"""


def _headers() -> Dict[str, str]:
    from .. import api_client

    token = api_client.get_token()
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _base() -> str:
    from .. import api_client

    return api_client.get_base_url()


def _extract(name: str, body: Any) -> Any:
    """从各端点不同的响应结构里取出「条目列表」。

    三种结构：
      · 标准信封   {success, data, error}          → data
      · 文件下载   {exported_at, count, resumes}   → resumes
      · 裸 dict    （salary 的 data 本身就是 dict）→ 原样
    """
    if name == "resumes":
        if isinstance(body, dict) and "resumes" in body:
            return body.get("resumes") or []
        raise PullError("简历导出返回结构异常（缺 resumes 字段）")
    if isinstance(body, dict) and "success" in body:
        if not body.get("success"):
            raise PullError(str(body.get("error") or "服务器返回 success=false"))
        return body.get("data")
    return body


def pull_one(name: str, timeout: Optional[int] = None) -> Dict[str, Any]:
    """拉取单路并写快照，返回 {source, count, path, fetched_at, items}。"""
    from .paths import ensure_dirs

    ensure_dirs()
    label, path, default_timeout = _ROUTES[name]
    url = _base() + path
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout or default_timeout)
    except requests.exceptions.RequestException as exc:
        raise PullError(f"{label} 拉取失败：{type(exc).__name__}: {exc}") from exc

    if resp.status_code == 401:
        raise PullError(f"{label} 拉取失败：登录已失效，请重新登录")
    if resp.status_code >= 400:
        raise PullError(f"{label} 拉取失败：HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise PullError(f"{label} 返回不是合法 JSON") from exc

    items = _extract(name, body)
    if isinstance(items, list):
        count = len(items)
    elif items is None:
        count = 0
    else:
        count = 1

    snapshot = {
        "source": name,
        "label": label,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "count": count,
        "items": items,
    }
    with open(cache_path(name), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return snapshot


def pull_all(timeout: Optional[int] = None) -> Dict[str, Any]:
    """拉取四路。单路失败不阻断其余（结果里带 error，便于界面提示）。"""
    result: Dict[str, Any] = {}
    for name in SOURCES:
        try:
            snap = pull_one(name, timeout=timeout)
            result[name] = {
                "ok": True,
                "label": snap["label"],
                "count": snap["count"],
                "fetched_at": snap["fetched_at"],
            }
        except PullError as exc:
            result[name] = {"ok": False, "label": _ROUTES[name][0], "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            result[name] = {
                "ok": False,
                "label": _ROUTES[name][0],
                "error": f"{type(exc).__name__}: {exc}",
            }
    return result


def load_cache(name: str) -> Optional[Dict[str, Any]]:
    """读取本地快照；不存在返回 None。"""
    p = cache_path(name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def cache_status() -> List[Dict[str, Any]]:
    """四路快照现状（不联网），供 status() 展示「本地数据截至何时」。"""
    out: List[Dict[str, Any]] = []
    for name in SOURCES:
        snap = load_cache(name)
        out.append({
            "source": name,
            "label": _ROUTES[name][0],
            "cached": snap is not None,
            "count": (snap or {}).get("count", 0),
            "fetched_at": (snap or {}).get("fetched_at"),
        })
    return out


__all__ = ["SOURCES", "PullError", "pull_one", "pull_all", "load_cache", "cache_status"]
