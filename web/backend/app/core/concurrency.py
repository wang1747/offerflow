"""异步化与并发控制中枢（零重依赖，可被任意模块安全导入）。

背景：
- 后端是 FastAPI（单事件循环），但 DB 访问是同步 SQLAlchemy、LLM 调用是阻塞式
  ``llm.invoke(...)``。在 ``async def`` 路径里直接跑这些阻塞调用会占死整个事件循环，
  期间所有其它请求（登录、语料浏览等）全部卡住；并发简历生成还会把平台
  50k token/日的配额一次性打穿、并在 1.6GB 小机上造成资源抖动。
- 本模块提供三个工具把阻塞调用移出事件循环，并对重路径加并发闸门：
  - ``ainvoke_llm``：把阻塞式 LLM invoke 丢到线程池。
  - ``run_sync``：把任意同步阻塞调用（同步 DB、向量检索等）丢到线程池。
  - ``generation_semaphore``：限制并发简历生成数量，保护小机与配额。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# 并发上限：默认 2。可用环境变量 RESUME_GEN_MAX_CONCURRENCY 覆盖（与 config.py 的
# resume_gen_max_concurrency 字段读取同一变量）。1.6GB 小机建议 1~2；LLM 走远端 API，
# 主要约束是配额与事件循环而非内存，故 2 在吞吐与资源间较平衡。
_GEN_CONCURRENCY = int(os.getenv("RESUME_GEN_MAX_CONCURRENCY", "2"))

_semaphore: "asyncio.Semaphore | None" = None


def generation_semaphore() -> "asyncio.Semaphore":
    """返回全局（单事件循环内）简历生成并发信号量。

    延迟创建：必须在运行中的事件循环内首次调用，避免导入期错误地绑定循环。

    ⚠️ **单进程闸门，不是跨进程全局闸门**：
    本模块的信号量只作用于**当前进程的事件循环**。生产实测（2026-10-03 17:38）
    确认后端是**单 worker**——`run.py` 调 `uvicorn.run()` 未传 `workers=`（默认 1），
    容器内 `/proc` 只有 1 个 python 进程（PID 1 = `python run.py`），
    因此总并发上限确实 = ``_GEN_CONCURRENCY``（当前 2），不存在「2 × worker 数」放大。

    若将来为利用多核改成多 worker（``uvicorn --workers N``），此闸门会退化为
    per-worker 限制（总并发 ≈ N × ``_GEN_CONCURRENCY``）。届时**必须**改用
    Redis/PG 之类的跨进程限流，或直接用 ``RESUME_GEN_MAX_CONCURRENCY`` 近似折半，
    否则 1.6GB 小机会被并发放大打爆。改动 worker 数时务必同步复核此处。
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_GEN_CONCURRENCY)
    return _semaphore


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """把同步阻塞函数放到默认线程池执行，释放事件循环。

    用于同步 DB 会话、向量检索等会做网络/磁盘 I/O 的调用，使其不再阻塞事件循环。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def ainvoke_llm(llm: Any, messages: Any, **kwargs: Any) -> Any:
    """把阻塞式 LLM invoke 移到线程池，避免占住事件循环。

    langchain ``ChatOpenAI.invoke`` 是同步网络调用；在 ``async def`` 路径里直接调用
    会卡住整个事件循环。改用 ``await ainvoke_llm(llm, [msg, ...])`` 即可非阻塞化，
    调用方必须是 ``async def`` 且对返回值 ``await``。
    """
    return await run_sync(llm.invoke, messages, **kwargs)
