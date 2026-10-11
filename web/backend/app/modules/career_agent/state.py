"""``AgentStateV1`` —— Agent 在一次执行中的内部状态 ABI。

设计依据：`docs/agent/v0-agent-capability-boundary.md` 第 12 节。

为什么分两种形态（设计文档 21.1）：
- **根状态**用 ``TypedDict`` + ``Annotated`` reducer —— LangGraph 对状态 reducer
  和 Checkpoint 支持最直接。
- **嵌套结构**用 Pydantic ``BaseModel`` —— 运行时校验、JSON Schema、序列化更可靠。

**硬不变量**（第 12.5 节，写进代码而非只写文档）：
- State 里**禁止**出现 ORM 对象、SQLAlchemy Session、API Key、Token、
  完整简历正文、完整 JD 正文。
- 业务正文与工具原始结果存 Postgres，State 只保留引用、版本、摘要。
- ``schema_version`` 固定为 1；删字段或改语义属于破坏性变更，必须升 ``AgentStateV2``。

v0.2-alpha 说明：本期只做只读规划，``approval`` 恒为 None（无写操作），
``memory`` 为空（不实现长期记忆）。字段先按契约定义好，后续阶段直接启用。
"""
from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

SCHEMA_VERSION = 1


# ===========================================================================
# 状态枚举（第 12.3 节）
# ===========================================================================
RunStatus = Literal[
    "created",
    "loading_context",
    "recalling_memory",
    "planning",
    "tool_running",
    "waiting_confirmation",
    "composing",
    "completed",
    "failed",
    "cancelled",
]

#: 终态集合——任何节点失败都不能把状态留在 ``tool_running`` / ``planning``。
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


# ===========================================================================
# 嵌套结构（Pydantic）
# ===========================================================================
class ContextRef(BaseModel):
    """指向一个业务对象的引用（只存 id，不存正文）。"""

    type: Literal["resume", "jd", "application"]
    id: str
    version: Optional[int] = None
    updated_at: Optional[str] = None


class RunState(BaseModel):
    thread_id: str
    run_id: str
    user_id: str
    status: RunStatus = "created"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InputState(BaseModel):
    message: str = ""
    goal: Optional[str] = None
    entry_source: str = "unknown"
    context_refs: List[ContextRef] = Field(default_factory=list)


class ContextObject(BaseModel):
    """上下文中已解析出的业务对象摘要。"""

    type: str
    id: str
    version: Optional[int] = None
    company: Optional[str] = None
    position: Optional[str] = None
    summary: Dict[str, Any] = Field(default_factory=dict)


class MissingContext(BaseModel):
    """缺失的上下文——Agent 必须返回 needs_input，不允许猜测。"""

    type: str
    reason: str
    hint: Optional[str] = None


class ContextState(BaseModel):
    active_objects: List[ContextObject] = Field(default_factory=list)
    context_version: Optional[str] = None
    missing_context: List[MissingContext] = Field(default_factory=list)


class MemoryRecall(BaseModel):
    memory_id: str
    content: str
    score: float = 0.0
    #: 记忆类型（``preference`` / ``decision`` / ``outcome``）。
    #: 模型需要它来区分"用户的偏好"与"过去对某个岗位的判断"——两者对规划的含义不同。
    #: 字段为**追加**（可选、有默认值），不构成破坏性变更，``schema_version`` 仍为 1。
    memory_type: str = "preference"


class MemoryRecord(BaseModel):
    """记忆记录。v0.2-alpha 不读写长期记忆，此结构先固定。"""

    memory_id: Optional[str] = None
    memory_type: Literal["preference", "decision", "outcome"] = "preference"
    status: Literal["proposed", "confirmed", "expired", "superseded"] = "proposed"
    content: str = ""
    source: Literal["user_explicit", "user_confirmed", "system_event"] = "system_event"
    confidence: float = 0.0
    created_at: Optional[str] = None
    expires_at: Optional[str] = None


class MemoryState(BaseModel):
    recalled: List[MemoryRecall] = Field(default_factory=list)
    confirmed_preferences: List[MemoryRecord] = Field(default_factory=list)
    proposed: List[MemoryRecord] = Field(default_factory=list)


class ActionCandidate(BaseModel):
    """建议行动。

    alpha 阶段**只是建议**；beta 起，``type`` 命中写工具白名单的行动会被
    ``prepare_write_action`` 转成 ``ActionIntent`` 并等用户确认——但**仍然不会**
    由模型直接执行（设计文档 9.2）。
    """

    type: str
    title: str
    priority: int = 1
    reason: str = ""
    evidence_refs: List[str] = Field(default_factory=list)
    requires_confirmation: bool = True
    #: 写行动的参数（如 ``{"new_status": "applied"}``）。
    #: ⚠️ 模型**只能**通过它提议目标状态；``application_id`` 与 ``expected_status``
    #: 一律由服务端从上下文取（见 ``graph._write_plan``）——让模型自己填期望值
    #: 等于让它决定"我基于哪个版本写"，CAS 就失效了。
    #: 字段为**追加**（可选、默认空），不构成破坏性变更，``schema_version`` 仍为 1。
    arguments: Dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """证据——必须能追溯到具体业务对象版本。"""

    ref: str
    source: str
    summary: str = ""
    object_version: Optional[str] = None


class DecisionState(BaseModel):
    intent: Optional[str] = None
    candidate_actions: List[ActionCandidate] = Field(default_factory=list)
    selected_plan: Optional[Dict[str, Any]] = None
    evidence: List[Evidence] = Field(default_factory=list)
    confidence: float = 0.0
    missing_info: List[str] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
    """每次工具调用的审计记录（第 13.5 节）。"""

    call_id: str
    tool_name: str
    tool_version: int = 1
    args_summary: str = ""
    status: str = "succeeded"
    result_ref: Optional[str] = None
    #: 结果摘要（**有界**、只含结构化摘要，绝不含 ORM / 完整正文）。
    #: 存在的理由：设计文档 20.3 要求 ``summarize_tool_results`` 之后还有一次
    #: ``model`` 调用，模型必须看到**每个**工具真正返回了什么；只靠
    #: ``tool_loop.last_result`` 只能留下最后一次调用。字段是**追加**的
    #: （可选、默认空），不构成破坏性变更，故 ``schema_version`` 仍为 1。
    result_summary: str = ""
    object_refs: List[ContextRef] = Field(default_factory=list)
    error_code: Optional[str] = None
    retryable: bool = False
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    latency_ms: int = 0


class ToolLoopState(BaseModel):
    calls: List[ToolCallRecord] = Field(default_factory=list)
    iteration: int = 0
    last_result: Optional[Dict[str, Any]] = None
    consecutive_error_count: int = 0


class ActionIntent(BaseModel):
    """写操作意图。alpha 阶段不产生，但结构先固定（第 10 节）。"""

    action_id: str
    type: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    status: Literal[
        "pending_confirmation", "confirmed", "executing",
        "succeeded", "failed", "expired", "cancelled",
    ] = "pending_confirmation"
    created_by: Literal["agent"] = "agent"
    source_context_version: Optional[str] = None
    expected_version: Optional[int] = None
    idempotency_key: Optional[str] = None
    expires_at: Optional[str] = None


class ApprovalState(BaseModel):
    """v0.2-alpha 恒为 None——没有写操作就没有确认流程。"""

    pending_action: Optional[ActionIntent] = None
    decision: Optional[str] = None
    idempotency_key: Optional[str] = None
    expires_at: Optional[datetime] = None


class OutputState(BaseModel):
    message: str = ""
    actions: List[ActionCandidate] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    missing_info: List[str] = Field(default_factory=list)


class LimitsState(BaseModel):
    """硬限制。设计文档 16 节：防无限循环与成本失控。"""

    max_model_turns: int = 4        # alpha 上限 4（完整 v0.2 为 8）
    max_tool_calls: int = 3         # alpha 上限 3（完整 v0.2 为 6）
    token_budget: int = 15000
    max_steps: int = 6
    timeout_seconds: int = 60       # alpha 单次运行总超时


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ObservabilityState(BaseModel):
    trace_id: Optional[str] = None
    #: 本次运行所用上下文的指纹（由 ``ContextBuilder`` 计算）。
    #: 阶段 4 验收要求 API 返回 context hash（设计文档 22.1），
    #: 因此把它从 ``trace_id`` 的字符串前缀里"解出来"改成独立字段——
    #: 前缀解析是隐式约定，改格式就会静默失效。
    #: 字段是**追加**的（可选、默认 None），不构成破坏性变更，``schema_version`` 仍为 1。
    context_hash: Optional[str] = None
    prompt_versions: List[str] = Field(default_factory=list)
    model: Optional[str] = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0


class AgentError(BaseModel):
    code: str
    message: str
    tool_name: Optional[str] = None
    retryable: bool = False
    at: Optional[str] = None


# ===========================================================================
# 根状态（TypedDict + reducer）
# ===========================================================================
class AgentStateV1(TypedDict):
    """Agent 一次执行的状态。

    Reducer 规则（12.4）：
    - ``messages``：``add_messages`` 追加，不覆盖历史窗口
    - ``tool_loop.calls`` / ``errors``：``operator.add`` 追加，保留全部历史
    - ``decision`` / ``approval`` / ``output``：最后写入值生效
    - ``context`` / ``memory``：每轮可整体替换，但须保留本轮 context snapshot 版本
    """

    schema_version: Literal[1]

    run: RunState
    input: InputState
    context: ContextState
    memory: MemoryState
    messages: Annotated[List[Any], add_messages]
    decision: Optional[DecisionState]
    tool_loop: ToolLoopState
    approval: Optional[ApprovalState]
    output: Optional[OutputState]
    limits: LimitsState
    observability: ObservabilityState
    errors: Annotated[List[AgentError], operator.add]


def new_agent_state(
    thread_id: str,
    run_id: str,
    user_id: str,
    limits: Optional[LimitsState] = None,
) -> AgentStateV1:
    """构造一份初始状态（``status="created"``）。

    ``user_id`` 只能由运行时传入——前端与模型都不得指定（设计文档 3 节）。
    """
    now = datetime.now(timezone.utc)
    return AgentStateV1(
        schema_version=SCHEMA_VERSION,
        run=RunState(
            thread_id=thread_id,
            run_id=run_id,
            user_id=user_id,
            status="created",
            started_at=now,
            updated_at=now,
        ),
        input=InputState(),
        context=ContextState(),
        memory=MemoryState(),
        messages=[],
        decision=None,
        tool_loop=ToolLoopState(),
        approval=None,   # alpha 无写操作
        output=None,
        limits=limits or LimitsState(),
        observability=ObservabilityState(),
        errors=[],
    )
