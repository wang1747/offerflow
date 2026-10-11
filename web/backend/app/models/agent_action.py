"""``ActionIntent`` 持久化与审计（设计文档第 10 节）。

## 为什么它必须落库，而不是只活在 State 里

写操作的确认流程是「Agent 提议 → 用户确认 → 领域模块执行」。这条链路上有两个
必须跨越的东西：

- **进程重启**：用户可能过几分钟才点确认，期间容器可能被重启（本项目常态：
  1.6GB 小机 + 频繁发布）。意图若只在 Graph State 里，重启后无法核对
  「用户确认的到底是哪一次提议」。
- **幂等**：LangGraph 的 ``interrupt()`` 恢复时**会从该节点开头重放**，
  也就是说 ``prepare_write_action`` 里"创建意图"这段代码会执行两次。
  用 ``idempotency_key`` 唯一索引把第二次变成读操作，是唯一可靠的解法。

## 状态流转（10 节）

``pending_confirmation → confirmed → executing → succeeded | failed | expired | cancelled``

**只有用户确认后才能进 ``confirmed``**（10 节原话）。领域模块执行成功后返回新的
业务版本，Agent 不得自行构造成功结果——所以 ``result`` 只由 capability 的返回值填充。
"""
from __future__ import annotations

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.sql import func

from ..core.database import Base

#: 意图状态（设计文档 10 节的状态流转）
ACTION_STATUSES = (
    "pending_confirmation", "confirmed", "executing",
    "succeeded", "failed", "expired", "cancelled",
)

#: 已经"落定"的终态——再次确认同一意图不再改变结果
ACTION_TERMINAL_STATUSES = ("succeeded", "failed", "expired", "cancelled")


class AgentActionIntent(Base):
    __tablename__ = "agent_action_intents"

    action_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=False)
    #: 契约名（``application.update_status``）——不是线名，与审计/证据 ref 一致
    type = Column(String, nullable=False)
    arguments = Column(JSON, nullable=False, default=dict)
    reason = Column(Text, nullable=False, default="")
    status = Column(String, nullable=False, default="pending_confirmation", index=True)
    source_context_version = Column(String, nullable=True)
    #: CAS 期望值（对本项目是期望的当前投递状态字符串，见 application/capabilities.py）
    expected_version = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=False)
    #: capability 返回的新业务版本；**Agent 不构造**，只透传
    result = Column(JSON, nullable=True)
    error_code = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # 幂等键：同一用户同一动作只允许存在一条。
        # 唯一约束而不是"查询后再判断"——后者在重放竞态下会双写。
        UniqueConstraint("user_id", "idempotency_key", name="uq_agent_action_idem"),
        Index("ix_agent_action_user_status", "user_id", "status"),
    )
