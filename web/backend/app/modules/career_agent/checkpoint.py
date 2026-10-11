"""Checkpointer 工厂（设计文档 21.2 / 21.5）。

v0.2-alpha 用 ``InMemorySaver``：进程重启即丢，这是 alpha 明确允许的。
v0.2-beta 换成 ``PostgresSaver``，它同时承担两件事：

1. **跨重启恢复会话**——进程重启后同一个 ``thread_id`` 的历史仍在；
2. **让 ``interrupt()`` 真的可用**——L 设计文档 21.5 要求写操作确认走 LangGraph
   原生 ``interrupt()`` 而不是自造 ``waiting_confirmation`` 节点。原生 interrupt
   的语义是「把图停在断点上，等外部用 ``Command(resume=...)`` 唤醒」，
   这**必须**有一个能在进程重启后读到断点的 Checkpointer；内存 saver 做不到
   （重启后断点消失，用户点了「确认」会恢复到一个不存在的线程）。

## 为什么不复用 SQLAlchemy 的 URL

项目的 ``DATABASE_URL`` 是 SQLAlchemy 形态：``postgresql+psycopg://...``。
psycopg 自己**不认** ``+psycopg`` 这个方言后缀，直接拿它建连接会解析失败。
转换函数见 :func:`to_psycopg_dsn`，并有单测钉住（``test_agent_beta.py``）。

## 为什么把 ``kind`` 暴露出来

``memory`` 是一个**危险的静默降级**：接口照常返回、会话照常能发消息，
只有"重启后会话消失"这一条会暴露它，而那通常发生在生产。因此
:func:`open_checkpointer` 返回带 ``kind`` 的结果，由 API 直接回给前端/验收
（``SessionView.checkpointer``），让"到底有没有持久化"变成**可观测**的事实。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: SQLAlchemy 里 Postgres 方言后缀 → psycopg 原生 scheme
_SQLALCHEMY_PG_PREFIXES = (
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql+asyncpg://",
    "postgres+psycopg://",
)

CHECKPOINTER_POSTGRES = "postgres"
CHECKPOINTER_MEMORY = "memory"


def to_psycopg_dsn(database_url: str) -> str:
    """把 SQLAlchemy 的 Postgres URL 转成 psycopg 能直接用的 DSN。

    非 Postgres（sqlite 等）原样返回——调用方会因此走内存 saver。
    """
    url = (database_url or "").strip()
    for prefix in _SQLALCHEMY_PG_PREFIXES:
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def is_postgres_url(database_url: str) -> bool:
    """是否是 Postgres（决定用持久化 saver 还是内存 saver）。"""
    return (database_url or "").strip().startswith("postgres")


@dataclass
class CheckpointerHandle:
    """一个已就绪的 Checkpointer 及其归属类型。"""

    saver: Any
    kind: str
    #: 需要显式关闭的资源（连接池）；内存 saver 为 None
    closer: Optional[Any] = None
    dsn: Optional[str] = None

    @property
    def is_persistent(self) -> bool:
        return self.kind == CHECKPOINTER_POSTGRES

    async def aclose(self) -> None:
        """关闭连接池。失败只记日志——关停路径不该因为清理失败而抛。"""
        if self.closer is None:
            return
        try:
            await self.closer.close()
        except Exception:  # noqa: BLE001
            logger.warning("关闭 Checkpointer 连接池失败", exc_info=True)


async def open_postgres_checkpointer(
    database_url: str,
    *,
    max_size: int = 4,
) -> CheckpointerHandle:
    """建立 ``AsyncPostgresSaver`` 并确保建表。

    ``max_size=4`` 是**内存预算**决定（服务器 1.6GB）：每个 PG 连接约 2~3MB，
    池上限 4 个 ≈ 10MB，对 744MB 可用内存是安全的。这个池**与业务
    SQLAlchemy 引擎相互独立**——Checkpointer 只存执行状态，不碰业务表
    （设计文档 15 节：Checkpoint / 会话消息 / 长期记忆三者不混淆事实源）。

    ``autocommit=True`` 与 ``prepare_threshold=0`` 是 langgraph 官方要求的
    连接参数：前者让 setup/写入立即生效，后者避开服务端 prepared statement
    与 PgBouncer 类中间件的兼容问题。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    dsn = to_psycopg_dsn(database_url)
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
    except Exception:
        await pool.close()
        raise
    return CheckpointerHandle(
        saver=saver, kind=CHECKPOINTER_POSTGRES, closer=pool, dsn=dsn,
    )


def open_memory_checkpointer() -> CheckpointerHandle:
    """内存 saver。**只用于本地测试与评测**，生产不得走这条路径。"""
    from langgraph.checkpoint.memory import InMemorySaver

    return CheckpointerHandle(saver=InMemorySaver(), kind=CHECKPOINTER_MEMORY)


async def open_checkpointer(database_url: str) -> CheckpointerHandle:
    """按 ``DATABASE_URL`` 选择 saver。

    非 Postgres（sqlite / 空）**明确**退回内存 saver 并打 WARNING——
    不是静默降级：调用方拿到 ``kind="memory"`` 并把它暴露到 API 响应上，
    "没有持久化"这件事因此永远看得见。
    """
    if not is_postgres_url(database_url):
        logger.warning(
            "DATABASE_URL 不是 Postgres（%s），Agent 会话将使用内存 Checkpointer："
            "进程重启后会话与 interrupt 断点都会丢失",
            (database_url or "").split("://", 1)[0] or "<empty>",
        )
        return open_memory_checkpointer()
    return await open_postgres_checkpointer(database_url)
