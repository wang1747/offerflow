"""mem0 记忆层接入 OfferFlow（带优雅降级）。

接入点：所有 agent / corpus 调用前，用 add() 写入用户偏好、投递历史、常问问题；
检索时用 search() 取回跨会话记忆，注入 prompt。

- 环境变量 MEM0_ENABLED=true 且 mem0 可导入时，走真实 mem0（后端用 PG/Chroma）。
- 否则退化为进程内 dict，保证主流程不报错。
- bid_tool（Node）通过同一份 mem0-server HTTP 接口复用（见 integration_adapters.md）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("offerflow.memory")

MEM0_ENABLED = os.getenv("MEM0_ENABLED", "false").lower() == "true"
# 内存护栏：1.6GB 小机可用内存 < 此阈值时强制禁用真实 mem0，避免 LLM 抽取/向量写入拖垮整机
_MEM_GUARD_MB = 1200.0
_mem0 = None  # None=未初始化; False=不可用
_fallback: Dict[str, List[Dict[str, Any]]] = {}


def _free_mem_mb() -> float:
    """读取 /proc/meminfo 的 MemAvailable，单位 MB。读取失败返回大值（不拦截）。"""
    try:
        with open("/proc/meminfo") as f:
            info = dict(line.split(":", 1) for line in f if ":" in line)
        val = info.get("MemAvailable", info.get("MemFree", "0")).strip().split()[0]
        return int(val) / 1024.0
    except Exception:
        return 9999.0


def _get_mem0():
    global _mem0
    if _mem0 is not None:
        return _mem0
    if not MEM0_ENABLED:
        _mem0 = False
        return _mem0
    if _free_mem_mb() < _MEM_GUARD_MB:
        log.warning("可用内存 %.0fMB < %sMB，禁用 mem0 真实后端（防拖垮小机）", _free_mem_mb(), _MEM_GUARD_MB)
        _mem0 = False
        return _mem0
    try:
        from mem0 import Memory

        _mem0 = Memory.from_config(
            {
                "version": "v1.1",
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": os.getenv("MEM0_EMBED_MODEL", "text-embedding-3-small"),
                        "api_key": os.getenv("OPENAI_API_KEY", ""),
                    },
                },
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": os.getenv("MEM0_QDRANT_HOST", "localhost"),
                        "port": int(os.getenv("MEM0_QDRANT_PORT", "6333")),
                    },
                },
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": os.getenv("MEM0_LLM", "gpt-4o-mini"),
                        "api_key": os.getenv("OPENAI_API_KEY", ""),
                    },
                },
            }
        )
        log.info("mem0 记忆层已启用（真实后端）")
        return _mem0
    except Exception as e:  # 离线/缺依赖：降级
        log.warning("mem0 不可用，降级为进程内记忆: %s", e)
        _mem0 = False
        return _mem0


def add(user_id: str, text: str, **kwargs: Any) -> None:
    """写入一条记忆（用户偏好 / 历史 / 结论）。"""
    m = _get_mem0()
    if m:
        try:
            m.add(text, user_id=user_id, **kwargs)
            return
        except Exception as e:
            log.warning("mem0.add 失败，降级内存: %s", e)
    _fallback.setdefault(user_id, []).append({"text": text, **kwargs})


def search(user_id: str, query: str, limit: int = 5) -> List[Any]:
    """检索该用户相关记忆，注入 prompt。"""
    m = _get_mem0()
    if m:
        try:
            res = m.search(query, user_id=user_id, limit=limit)
            return res.get("results", []) if isinstance(res, dict) else res
        except Exception as e:
            log.warning("mem0.search 失败，降级内存: %s", e)
    return _fallback.get(user_id, [])[:limit]


def recall_as_text(user_id: str, query: str, limit: int = 5) -> str:
    """把记忆拼成可注入 prompt 的文本块（无记忆返回空串）。"""
    items = search(user_id, query, limit)
    if not items:
        return ""
    lines = []
    for it in items:
        if isinstance(it, dict):
            lines.append("- " + str(it.get("text") or it.get("memory") or it.get("data") or it))
        else:
            lines.append("- " + str(it))
    return "\n".join(lines)


def memory_status() -> Dict[str, Any]:
    """返回 mem0 记忆层当前状态，供 /integrations/status 聚合。"""
    try:
        import mem0  # noqa: F401
        lib_available = True
    except Exception:
        lib_available = False
    using_real = bool(MEM0_ENABLED) and lib_available and _free_mem_mb() >= _MEM_GUARD_MB and _mem0 is not False
    return {
        "enabled": MEM0_ENABLED,
        "library_available": lib_available,
        "active": using_real,
        "backend": "mem0" if using_real else "in_process_fallback",
        "free_mem_mb": round(_free_mem_mb(), 1),
        "guard_mb": _MEM_GUARD_MB,
    }
