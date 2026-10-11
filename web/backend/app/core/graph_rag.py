"""LightRAG 图谱检索接入（多跳推理），优雅降级到现有向量检索。

安全加固（防小机被拖死，2026-09-30 事故后补）：
- 任何 LightRAG 调用都有硬超时（GRAPH_TIMEOUT），超时即降级，绝不无限阻塞。
- 失败/超时累计达到阈值后熔断，进程生命周期内不再重试初始化。
- 机器可用内存过低（< MEM_FLOOR_MB）时直接禁用，不初始化——1.6GB 小机常态命中。
- graph_status 不触发初始化（只读开关与熔断状态），避免查状态时顺手建图。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import List, Dict, Optional

from sqlalchemy.orm import Session

log = logging.getLogger("offerflow.graph_rag")

LIGHTRAG_ENABLED = os.getenv("LIGHTRAG_ENABLED", "false").lower() == "true"
_WORKING_DIR = os.getenv("LIGHTRAG_WORKING_DIR", "./lightrag_storage")

# ---- 安全参数（均可用环境变量覆盖）----
GRAPH_TIMEOUT = float(os.getenv("LIGHTRAG_TIMEOUT", "20"))          # 单次调用硬超时（秒）
FAIL_BREAK_THRESHOLD = int(os.getenv("LIGHTRAG_FAIL_THRESHOLD", "2"))  # 连续失败几次后熔断
MEM_FLOOR_MB = int(os.getenv("LIGHTRAG_MEM_FLOOR_MB", "1200"))      # 可用内存低于此值不启用

_rag = None          # None=未初始化; False=不可用/已熔断
_fail_count = 0
_fail_lock = threading.Lock()


def _available_memory_mb() -> float:
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                k, _, v = line.partition(":")
                if k.strip() in ("MemAvailable", "MemFree"):
                    info[k.strip()] = int(v.strip().split()[0])  # KB
        return info.get("MemAvailable", info.get("MemFree", 0)) / 1024.0
    except Exception:
        return 99999.0


def _memory_too_low() -> bool:
    return _available_memory_mb() < MEM_FLOOR_MB


def _build_llm_func():
    from langchain_core.messages import HumanMessage, SystemMessage
    from app.core.llm import get_llm

    async def llm_func(prompt: str, system_prompt=None, history_messages=None, **kwargs) -> str:
        msgs = []
        if system_prompt:
            msgs.append(SystemMessage(content=system_prompt))
        msgs.append(HumanMessage(content=prompt))
        resp = await get_llm().ainvoke(msgs)
        return resp.content if hasattr(resp, "content") else str(resp)

    return llm_func


def _build_embed_func():
    from app.core.embeddings import embed_text, embed_hash_fallback

    async def embed_func(texts: List[str]) -> List[List[float]]:
        loop = asyncio.get_event_loop()
        out: List[List[float]] = []
        for t in texts:
            vec = await loop.run_in_executor(None, embed_text, t)
            if vec is None:
                vec = embed_hash_fallback(t)
            out.append(vec)
        return out

    return embed_func


def _build_rag():
    global _rag
    if not LIGHTRAG_ENABLED:
        _rag = False
        return None
    if _rag is not None:
        return _rag if _rag else None
    if _memory_too_low():
        log.warning("[graph_rag] 可用内存不足 %dMB，禁用 LightRAG（防小机卡死）", MEM_FLOOR_MB)
        _rag = False
        return None
    try:
        from lightrag import LightRAG, QueryParam  # noqa: F401

        os.makedirs(_WORKING_DIR, exist_ok=True)
        _rag = LightRAG(
            working_dir=_WORKING_DIR,
            llm_model_func=_build_llm_func(),
            embedding_func=_build_embed_func(),
        )
        log.info("[graph_rag] LightRAG 初始化成功 (working_dir=%s)", _WORKING_DIR)
        return _rag
    except Exception as e:
        log.warning("[graph_rag] 初始化失败，降级向量检索: %s", e)
        _rag = False
        return None


def _safe_call(fn, *args, timeout=None):
    """在线程里跑可能阻塞的同步调用；若是协程则 asyncio.run。
    返回 ('ok', result) / ('timeout', None) / ('err', exc)。"""
    timeout = timeout or GRAPH_TIMEOUT
    box: dict = {}

    def target():
        try:
            r = fn(*args)
            if asyncio.iscoroutine(r):
                r = asyncio.run(r)
            box["r"] = ("ok", r)
        except Exception as e:  # noqa: BLE001
            box["r"] = ("err", e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return ("timeout", None)
    return box.get("r", ("err", RuntimeError("no result")))


def _on_failure():
    global _fail_count, _rag
    with _fail_lock:
        _fail_count += 1
        if _fail_count >= FAIL_BREAK_THRESHOLD:
            log.warning("[graph_rag] 连续失败 %d 次，熔断 LightRAG（进程内不再重试）", _fail_count)
            _rag = False  # 永久降级


def query_graph(
    query_text: str,
    top_k: int = 5,
    mode: str = "hybrid",
    db: Optional[Session] = None,
    viewer_user_id: Optional[str] = None,
    include_private: bool = False,
    is_admin: bool = False,
) -> List[Dict]:
    """多跳/混合检索。LightRAG 可用走图谱，否则降级向量检索。"""
    rag = _build_rag()
    if rag is not None:
        try:
            from lightrag import QueryParam

            status, resp = _safe_call(rag.query, query_text, param=QueryParam(mode=mode, top_k=top_k))
            if status == "ok":
                content = resp if isinstance(resp, str) else str(resp)
                return [{"content": content, "source": "lightrag", "score": 1.0}]
            if status == "timeout":
                log.warning("[graph_rag] 查询超时(%ss)，降级向量检索", GRAPH_TIMEOUT)
            else:
                log.warning("[graph_rag] 查询失败，降级向量检索: %s", resp)
            _on_failure()
        except Exception as e:  # noqa: BLE001
            log.warning("[graph_rag] 查询异常，降级向量检索: %s", e)
            _on_failure()
    try:
        from app.modules.corpus.services import retrieve_similar

        return retrieve_similar(
            query_text=query_text,
            top_k=top_k,
            db=db,
            viewer_user_id=viewer_user_id,
            include_private=include_private,
            is_admin=is_admin,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[graph_rag] 降级检索也失败: %s", e)
        return []


def insert_text(text: str, doc_id: Optional[str] = None) -> bool:
    """向图谱插入文档（建图）。失败/超时静默返回 False，并可能触发熔断。"""
    rag = _build_rag()
    if rag is None:
        return False
    status, _ = _safe_call(rag.insert, text)
    if status == "ok":
        return True
    if status == "timeout":
        log.warning("[graph_rag] 插入超时(%ss)，降级", GRAPH_TIMEOUT)
    else:
        log.warning("[graph_rag] 插入失败，降级")
    _on_failure()
    return False


def graph_status() -> dict:
    """只读开关与熔断状态，不触发初始化（避免查状态时建图卡死）。"""
    return {
        "enabled": LIGHTRAG_ENABLED,
        "active": _rag is not None and _rag is not False,
        "circuit_broken": bool(LIGHTRAG_ENABLED and _rag is False),
        "mem_too_low": _memory_too_low(),
    }
