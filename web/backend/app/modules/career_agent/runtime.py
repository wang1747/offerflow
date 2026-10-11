"""Agent v0.2-beta 运行时：Checkpointer 生命周期 + Graph 单例。

为什么单独抽一个模块：**Graph 与它的 Checkpointer 必须同生命周期**。
如果图是模块级单例、Checkpointer 却在 lifespan 里被替换，就会出现
"注册表指向会话 A，图却去查另一个 saver"的错位，而且只在特定启动顺序下复现。

## 两条路径

| 路径 | 谁用 | Checkpointer |
|---|---|---|
| ``init_agent_runtime()`` | 生产（``main.lifespan``） | PostgresSaver（持久） |
| 惰性兜底 | 本地测试、评测脚本 | InMemorySaver（**并打 WARNING**） |

惰性兜底**故意保留**：测试与评测脚本不跑 FastAPI lifespan，若强行要求显式初始化，
所有既有用例都要改。但它不是静默降级——:func:`checkpointer_status` 会把
``initialized=False`` / ``kind="memory"`` 如实报给 API，验收可以直接断言。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.config import get_settings
from app.modules.career_agent.checkpoint import (
    CheckpointerHandle,
    open_checkpointer,
    open_memory_checkpointer,
)
from app.modules.career_agent.graph import build_agent_graph

logger = logging.getLogger(__name__)

#: 显式初始化后的 handle；None 表示"没初始化过"（测试/评测路径）
_handle: Optional[CheckpointerHandle] = None
#: 与 _handle 配套的图实例
_graph: Optional[Any] = None


def get_agent_graph():
    """取 Agent 图；未显式初始化时惰性建一个**内存**图并留日志。"""
    global _graph
    if _graph is None:
        logger.warning(
            "Agent 运行时未显式初始化，使用内存 Checkpointer——"
            "仅适用于测试/评测，生产必须经 init_agent_runtime() 初始化"
        )
        _graph = build_agent_graph(open_memory_checkpointer().saver)
    return _graph


async def init_agent_runtime(database_url: Optional[str] = None) -> str:
    """按 ``DATABASE_URL`` 建立 Checkpointer 与图。返回 checkpointer 类型。"""
    global _handle, _graph
    await shutdown_agent_runtime()

    url = database_url or get_settings().database_url
    handle = await open_checkpointer(url)
    _handle = handle
    _graph = build_agent_graph(handle.saver)
    logger.info(
        "Agent 运行时已初始化：checkpointer=%s persistent=%s",
        handle.kind, handle.is_persistent,
    )
    return handle.kind


async def shutdown_agent_runtime() -> None:
    """关闭连接池并丢弃单例。可重复调用。"""
    global _handle, _graph
    if _handle is not None:
        await _handle.aclose()
    _handle = None
    _graph = None


def checkpointer_status() -> Dict[str, Any]:
    """供 API/验收读取的 Checkpointer 事实（**不猜、不美化**）。"""
    if _handle is None:
        return {"kind": "uninitialized", "persistent": False, "initialized": False}
    return {
        "kind": _handle.kind,
        "persistent": _handle.is_persistent,
        "initialized": True,
    }


def checkpointer_kind() -> str:
    return checkpointer_status()["kind"]


def reset_agent_runtime_for_tests() -> None:
    """仅测试：丢弃单例。

    **不关连接池**：测试路径用的是生而内存的 saver，没有需要释放的连接；
    真要测关池行为请显式调用 ``shutdown_agent_runtime()``。
    """
    global _handle, _graph
    _handle = None
    _graph = None
