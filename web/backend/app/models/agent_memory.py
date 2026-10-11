"""长期记忆表（v0.2-beta），对应设计文档 15.1 ``MemoryRecordSchema``。

## 两条不可让步的规则

1. **``proposed`` 与 ``confirmed`` 是两件事**。模型/系统推断出来的只能写
   ``proposed``，必须由用户确认才变 ``confirmed``（15.2 节）。``recalled``
   只召回 ``confirmed``——把 proposed 当事实用就等于「模型自己给自己授权」。
2. **禁止进入长期记忆的数据**（15.3 节）：完整简历正文、完整 JD 正文、
   优化后的简历全文、密码/Token/API Key/Cookie、可实时查询到的数据副本、
   其他用户的数据。这些在写入层用**结构约束**拦住（见
   ``memory_repository.assert_memory_content_allowed``），不靠事后审计。

## 冲突处理

同一 user + 同 type 的新记忆与旧记忆冲突时，**保留新记录并把旧的标
``superseded``**（15.2 节），不静默覆盖。旧记录连同它的向量一起从召回面消失，
但行仍在表里——"这条偏好曾经存在过"必须留痕。
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, JSON, String, Text
from sqlalchemy.sql import func

from ..core.database import Base

#: 记忆类型（15.1）
MEMORY_TYPES = ("preference", "decision", "outcome")
#: 记忆状态（15.1）
MEMORY_STATUSES = ("proposed", "confirmed", "expired", "superseded")
#: 记忆来源（15.1）
MEMORY_SOURCES = ("user_explicit", "user_confirmed", "system_event")


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    memory_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    memory_type = Column(String, nullable=False, default="preference")
    status = Column(String, nullable=False, default="proposed", index=True)
    #: 给用户看的原始文本（有长度上限，写入层强制）
    content = Column(Text, nullable=False, default="")
    #: 参与向量化的规范化文本（去噪、统一大小写）。与 content 分开存，
    #: 是因为"给人看"与"给检索用"的文本会漂移（改了后者不该动前者）。
    normalized_content = Column(Text, nullable=False, default="")
    source = Column(String, nullable=False, default="system_event")
    #: 冲突键。同 user + 同冲突键的新记忆会**取代**旧记忆（旧的标 ``superseded``）。
    #: 为什么需要它：15.2 节要求"记忆冲突时保留新记录并把旧的标 superseded"，
    #: 但"冲突"不能靠语义相似度猜——那会把"我喜欢北京"和"我喜欢北京烤鸭"
    #: 误判成冲突。冲突是**业务语义**，必须由写入方显式声明（如 ``target_city``）。
    #: 为空表示这条记忆不参与取代（多条可共存）。
    conflict_key = Column(String, nullable=True)
    #: 支持该记忆的引用（evidence ref / 业务对象引用）。**空引用不允许写成 confirmed**。
    source_refs = Column(JSON, nullable=False, default=list)
    confidence = Column(Float, nullable=False, default=0.0)
    importance = Column(Float, nullable=False, default=0.5)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime, nullable=True)
    #: 被哪条新记忆取代（15.1）
    superseded_by = Column(String, nullable=True)

    __table_args__ = (
        # 召回的主路径：某用户的有效记忆
        Index("ix_agent_memories_user_status", "user_id", "status"),
        # 取代路径：同用户 + 同冲突键的现存有效记忆
        Index("ix_agent_memories_user_conflict", "user_id", "conflict_key"),
    )
