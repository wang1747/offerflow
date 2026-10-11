"""Agent 会话与运行索引（v0.2-beta）。

为什么必须有这两张表：alpha 的会话注册表是**进程内 OrderedDict**，因为 alpha 的
Checkpointer 也是内存的，两者同生命周期、一起消失。beta 换成 ``PostgresSaver`` 后
这个平衡被打破——checkpoint 跨重启活下来了，注册表却没了，于是出现
「thread 里有状态，但没人知道 session_id 属于谁」的悬空状态，
``GET /sessions/{id}`` 会 404、用户拿不回自己的会话。

设计文档第 15 节的四层存储边界里，这两张表属于「会话消息 / 运行索引」那一层：

| 层 | 存放 | 生命周期 |
|---|---|---|
| Graph 执行状态 | LangGraph Checkpointer（checkpoints 等表） | 单次运行 + 恢复周期 |
| 会话与运行索引 | **本文件的 agent_sessions / agent_runs** | 用户可配置保留期 |
| 长期记忆 | ``agent_memories`` | 长期 |
| 语义索引 | Chroma ``agent_memory`` 集合 | 随长期记忆同步 |

**这里不存业务真值，也不存完整消息正文**——只存会话元数据、上下文引用、
运行终态与工具调用摘要（与 ``RunView`` 同级的最小信息）。
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func

from ..core.database import Base


class AgentSession(Base):
    """一条 Agent 会话。``id`` **恒等于** LangGraph 的 ``thread_id``（设计文档 21.6）。"""

    __tablename__ = "agent_sessions"

    #: session_id == thread_id。直接作为主键，避免"再存一列 thread_id 然后两列漂移"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    entry_source = Column(String, nullable=False, default="unknown")
    #: ``[{"type": "jd", "id": "...", "version": null}, ...]``——只存引用，不存正文
    context_refs = Column(JSON, nullable=False, default=list)
    context_hash = Column(String, nullable=True, index=True)
    context_version = Column(String, nullable=True)
    missing_context = Column(JSON, nullable=False, default=list)
    warnings = Column(JSON, nullable=False, default=list)
    status = Column(String, nullable=False, default="created")
    #: 最近一次运行的 OutputView（dict）。用于会话恢复时直接展示上一次结论
    last_output = Column(JSON, nullable=True)
    #: 运行计数。**冗余存一列**而不是 count(agent_runs)：会话列表页要按它排序，
    #: 每次 count 会在 1.6GB 小机上放大查询成本。
    run_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentRun(Base):
    """一次运行（一条用户消息 = 一个 run_id）。Trace 的来源。"""

    __tablename__ = "agent_runs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(
        String, ForeignKey("agent_sessions.id"), nullable=False, index=True,
    )
    user_id = Column(String, nullable=False, index=True)
    #: 会话内序号，从 1 开始。前端按它排 Trace。
    run_index = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="completed")
    decision = Column(String, nullable=True)
    latency_ms = Column(Integer, nullable=False, default=0)
    context_hash = Column(String, nullable=True)
    #: ToolCallView 列表（工具名/状态/错误码/耗时），**不含工具原始返回**
    tool_calls = Column(JSON, nullable=False, default=list)
    errors = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now(), index=True)
