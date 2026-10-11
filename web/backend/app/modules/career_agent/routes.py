"""career_agent v0.2-beta API —— 在 alpha 只读切片上加入「确认写入」与「长期记忆」。

设计依据：
- **2.6 / 2.9 / 2.10** 会话规则、写操作确认、前端接口草案；
- **2.11** 同一会话同一时刻只能有一个正在执行的消息；
- **21.5** 写操作走 LangGraph 原生 ``interrupt()``，恢复用 ``Command(resume=...)``；
- **21.6** ``session_id == thread_id``，一条用户消息 = 一个独立 ``run_id``。

## beta 相对 alpha 的四点变化

1. **会话与运行落库**（``agent_sessions`` / ``agent_runs``）。alpha 用进程内
   ``OrderedDict``，因为那时 Checkpointer 也是内存的、两者一起消失；beta 换成
   ``PostgresSaver`` 后，注册表若还在内存，重启就会出现「断点在、会话找不到」。
2. **Checkpointer 类型暴露到响应**（``SessionView.checkpointer``）。
   ``memory`` 是个危险的静默降级——接口照常工作，只有"重启后会话消失"会暴露它。
3. **确认端点**：``POST /sessions/{id}/actions/{action_id}``。它 resume 被
   ``interrupt()`` 暂停的图。
4. **记忆端点**：列表 / 显式添加 / 确认 / 删除。

## 仍然守住的硬约束（与 alpha 相同）

- **跨用户统一 404**：他人会话、他人意图、他人记忆一律 ``not_found``，
  不区分"不存在"与"无权限"——避免用状态码差异探测存在性。
- **依赖 per-run 注入**：``make_deps`` + ``graph_config``，**没有任何模块级全局**。
- **模型不可见身份**：``user_id`` 只由运行时注入。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import QuotaExceededError
from app.core.llm import get_user_llm_or_raise
from app.models.user import User
from app.modules.auth.routes import get_current_user_required
from app.modules.career_agent.catalog import list_tools
from app.modules.career_agent.context import ContextBuilder
from app.modules.career_agent.graph import graph_config, make_deps
from app.modules.career_agent.memory import MemoryService
from app.modules.career_agent.memory_repository import (
    MemoryContentRejected,
    MemoryRepository,
)
from app.modules.career_agent.policies import (
    ActionIntentStore,
    check_resume_allowed,
)
from app.modules.career_agent.runtime import (
    checkpointer_status,
    get_agent_graph,
    reset_agent_runtime_for_tests,
)
from app.modules.career_agent.sessions import AgentSessionStore, refs_from_json
from app.modules.career_agent.state import (
    ActionCandidate,
    ContextRef,
    Evidence,
    LimitsState,
    MissingContext,
    new_agent_state,
)
from app.schemas.common import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["求职 Agent（v0.2-beta）"])

#: 单次运行总超时（20.3 节：60 秒）
RUN_TIMEOUT_SECONDS = 60


# ===========================================================================
# 请求 / 响应模型
# ===========================================================================
class CreateSessionRequest(BaseModel):
    """创建会话。只传 ID 与页面来源，**不传正文、不传用户信息**（2.6 节）。"""

    entry_source: str = Field(default="unknown", max_length=64)
    jd_id: Optional[str] = Field(default=None, max_length=128)
    resume_id: Optional[str] = Field(default=None, max_length=128)
    application_id: Optional[str] = Field(default=None, max_length=128)


class RunMessageRequest(BaseModel):
    """运行一条用户消息。

    **不接受上下文**：2.6 节规定「上下文变化时创建新会话，不能静默复用旧会话」。
    """

    message: str = Field(..., min_length=1, max_length=8000)


class ActionDecisionRequest(BaseModel):
    """用户对待确认写操作的裁决。``approved=False`` 表示拒绝。"""

    approved: bool


class CreateMemoryRequest(BaseModel):
    """用户**显式**添加一条偏好（8.2 节：可标 ``user_explicit`` 直接确认）。"""

    content: str = Field(..., min_length=1, max_length=500)
    conflict_key: Optional[str] = Field(default=None, max_length=64)


class ToolCallView(BaseModel):
    tool: str
    status: str
    error_code: Optional[str] = None
    latency_ms: int = 0


class OutputView(BaseModel):
    decision: Optional[str] = None
    confidence: float = 0.0
    summary: str = ""
    actions: List[ActionCandidate] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    missing_info: List[str] = Field(default_factory=list)


class ActionIntentView(BaseModel):
    """待确认写操作的**只读视图**。

    ``expected_status`` 与 ``new_status`` 都来自意图的参数（服务端填的），
    前端拿它渲染确认卡文案——不让前端自己拼。
    """

    action_id: str
    tool: str
    status: str
    application_id: Optional[str] = None
    expected_status: Optional[str] = None
    new_status: Optional[str] = None
    reason: str = ""
    expires_at: Optional[str] = None


class MemoryView(BaseModel):
    memory_id: str
    memory_type: str
    status: str
    content: str
    source: str
    confidence: float = 0.0
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    superseded_by: Optional[str] = None
    #: 来源引用（证据 ref / 业务对象）。**空引用的记忆写不成 confirmed**。
    source_refs: List[str] = Field(default_factory=list)


class WriteFailureView(BaseModel):
    """「确认了写操作，但没写成」的**结构化**原因。

    为什么不能只靠 ``errors`` 里的文案：``errors`` 是字符串，前端要区分
    「版本冲突（CAS 失败）」和「写库失败」就只能去匹配中文句子——那是**推断**，
    文案一改就静默失效。这里把 ``error_code``（``status_conflict`` /
    ``invalid_status`` / ``write_failed`` / …）原样带出去，让前端按取值分支。

    只在**写路径**上出现：``approval.decision == approved`` 且意图未落
    ``succeeded``。只读运行与用户主动取消都不会产生它。
    """

    code: str
    message: str
    tool: Optional[str] = None


class RunView(BaseModel):
    """一次运行的只读结果。"""

    session_id: str
    run_id: str
    status: str
    needs_input: bool = False
    output: OutputView = Field(default_factory=OutputView)
    missing_context: List[MissingContext] = Field(default_factory=list)
    tool_calls: List[ToolCallView] = Field(default_factory=list)
    context_hash: Optional[str] = None
    latency_ms: int = 0
    errors: List[str] = Field(default_factory=list)
    #: 非 None 表示这一轮**停在等用户确认**（status == waiting_confirmation）
    pending_action: Optional[ActionIntentView] = None
    #: 本轮是否发生过业务写入（只有用户确认过的写操作才会置 True）
    wrote_business_data: bool = False
    #: 写了但没写成的结构化原因；没走写路径就是 None（见 WriteFailureView）
    write_failure: Optional[WriteFailureView] = None
    #: 当前 Checkpointer 类型（``postgres`` / ``memory`` / ``uninitialized``）
    checkpointer: str = "uninitialized"


class SessionView(BaseModel):
    session_id: str
    #: **必须等于 session_id**（21.6）。单独返回是为了让前端与验收能直接核对。
    thread_id: str
    entry_source: str
    context_refs: List[ContextRef] = Field(default_factory=list)
    context_hash: Optional[str] = None
    context_version: Optional[str] = None
    missing_context: List[MissingContext] = Field(default_factory=list)
    needs_input: bool = False
    warnings: List[str] = Field(default_factory=list)
    status: str = "created"
    run_count: int = 0
    last_output: Optional[OutputView] = None
    created_at: str = ""
    updated_at: str = ""
    resumed: bool = False
    #: beta 起长期记忆启用
    memory_enabled: bool = True
    #: 这个会话当下是否有一个待确认的写操作
    has_pending_action: bool = False
    #: Checkpointer 事实（见文件头第 2 点）
    checkpointer: str = "uninitialized"


class ToolView(BaseModel):
    name: str
    wire_name: str
    version: int
    kind: str
    description: str
    evidence_policy: str
    required_scopes: List[str] = Field(default_factory=list)
    confirmation_required: bool = False
    idempotency_required: bool = False


class RunTrace(BaseModel):
    run_id: str
    status: str
    decision: Optional[str] = None
    latency_ms: int = 0
    context_hash: Optional[str] = None
    tool_calls: List[ToolCallView] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    created_at: str = ""


class TraceView(BaseModel):
    session_id: str
    runs: List[RunTrace] = Field(default_factory=list)


# ===========================================================================
# 进程内并发锁（**不是**会话存储）
# ===========================================================================
#: 同一会话同一时刻只允许一条消息在执行（2.11）。
#: 这是**进程内**锁：生产是单 worker（见 core/concurrency.py 的实测说明），
#: 所以它确实是全局限流；若将来改多 worker，这里会退化成 per-worker 限制，
#: 届时需要换成数据库层面的锁（如 ``SELECT ... FOR UPDATE``）。
_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _reset_agent_api_state() -> None:
    """仅供测试：丢弃进程内状态、Graph 单例与向量集合句柄。

    向量集合句柄也必须清：每个用例的 ``chroma_persist_path`` 指向各自的
    ``tmp_path``，缓存不清就会让第二个用例往第一个用例的目录里写记忆
    （症状是"明明只往 A 用户写了记忆，B 用户却召回得到"）。
    """
    from app.modules.career_agent.memory_repository import (
        _reset_collection_cache_for_tests,
    )

    _locks.clear()
    reset_agent_runtime_for_tests()
    _reset_collection_cache_for_tests()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_owned_session(session_id: str, user: User, db: Session):
    """取会话；**不存在与不属于当前用户返回同一个 404**。"""
    session = AgentSessionStore(db, user.id).get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在",
        )
    return session


# ===========================================================================
# 视图转换
# ===========================================================================
def _output_view(state_values: Dict[str, Any]) -> OutputView:
    decision = state_values.get("decision")
    output = state_values.get("output")
    if output is not None:
        return OutputView(
            decision=decision.intent if decision else None,
            confidence=decision.confidence if decision else 0.0,
            summary=output.message or "",
            actions=list(output.actions or []),
            evidence=list(output.evidence or []),
            missing_info=list(output.missing_info or []),
        )
    return OutputView(
        decision=decision.intent if decision else None,
        confidence=decision.confidence if decision else 0.0,
        summary="",
        actions=list(decision.candidate_actions or []) if decision else [],
        evidence=list(decision.evidence or []) if decision else [],
        missing_info=list(decision.missing_info or []) if decision else [],
    )


def _tool_call_views(state_values: Dict[str, Any]) -> List[ToolCallView]:
    """本次运行的工具调用视图。

    ⚠️ **不要**给它加"按基线切片"——``tool_loop`` 是**普通通道**（无 reducer），
    每次运行都会被 ``new_agent_state`` 里那份全新的 ``ToolLoopState()`` 替换。
    要切片的只有 ``errors``（``operator.add`` 会累积），见 ``_count_existing_errors``。
    """
    loop = state_values.get("tool_loop")
    if loop is None:
        return []
    return [
        ToolCallView(
            tool=c.tool_name, status=c.status,
            error_code=c.error_code, latency_ms=c.latency_ms,
        )
        for c in loop.calls
    ]


def _pending_action_view(state_values: Dict[str, Any]) -> Optional[ActionIntentView]:
    """从状态里取待确认意图——**没有就返回 None，不构造占位对象**。"""
    approval = state_values.get("approval")
    pending = getattr(approval, "pending_action", None) if approval is not None else None
    if pending is None:
        return None
    args = dict(pending.arguments or {})
    return ActionIntentView(
        action_id=pending.action_id,
        tool=pending.type,
        status=pending.status,
        application_id=args.get("application_id"),
        expected_status=args.get("expected_status"),
        new_status=args.get("new_status"),
        reason=pending.reason or "",
        expires_at=pending.expires_at,
    )


def _memory_view(row) -> MemoryView:
    return MemoryView(
        memory_id=row.memory_id,
        memory_type=row.memory_type,
        status=row.status,
        content=row.content,
        source=row.source,
        confidence=float(row.confidence or 0.0),
        created_at=row.created_at.isoformat() if row.created_at else None,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        superseded_by=row.superseded_by,
        source_refs=list(row.source_refs or []),
    )


def _session_view(
    session,
    store: AgentSessionStore,
    *,
    resumed: bool = False,
) -> SessionView:
    return SessionView(
        session_id=session.id,
        thread_id=session.id,               # 21.6：session_id == thread_id
        entry_source=session.entry_source,
        context_refs=refs_from_json(session.context_refs),
        context_hash=session.context_hash,
        context_version=session.context_version,
        missing_context=[
            MissingContext(**m) for m in (session.missing_context or [])
            if isinstance(m, dict)
        ],
        needs_input=bool(session.missing_context),
        warnings=list(session.warnings or []),
        status=session.status or "created",
        run_count=int(session.run_count or 0),
        last_output=OutputView(**session.last_output) if session.last_output else None,
        created_at=session.created_at.isoformat() if session.created_at else "",
        updated_at=session.updated_at.isoformat() if session.updated_at else "",
        resumed=resumed,
        memory_enabled=True,
        has_pending_action=store.pending_run(session.id) is not None,
        checkpointer=checkpointer_status()["kind"],
    )


# ===========================================================================
# 会话端点
# ===========================================================================
@router.post(
    "/sessions",
    response_model=ApiResponse[SessionView],
    summary="创建（或恢复）Agent 会话",
)
async def create_session(
    req: CreateSessionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    refs: List[ContextRef] = []
    if req.jd_id:
        refs.append(ContextRef(type="jd", id=req.jd_id))
    if req.resume_id:
        refs.append(ContextRef(type="resume", id=req.resume_id))
    if req.application_id:
        # beta 起投递记录是**一等上下文**（写操作需要它做 CAS 的期望值来源）
        refs.append(ContextRef(type="application", id=req.application_id))

    built = ContextBuilder(db, current_user.id).build(
        context_refs=refs,
        entry_source=req.entry_source,
    )
    store = AgentSessionStore(db, current_user.id)

    # 同一用户 + 同一 context hash ⇒ 复用（2.6 节）
    existing = store.find_by_context_hash(built.context_hash)
    if existing is not None:
        return ApiResponse(
            success=True,
            data=_session_view(existing, store, resumed=True),
            error=None,
        )

    session = store.create(
        session_id=uuid.uuid4().hex,
        entry_source=req.entry_source,
        context_refs=refs,
        built=built,
    )
    logger.info(
        "Agent 会话创建：user=%s session=%s refs=%s missing=%s",
        current_user.id, session.id,
        [(r.type, r.id) for r in refs], [m.type for m in built.missing_context],
    )
    return ApiResponse(
        success=True, data=_session_view(session, store), error=None,
    )


@router.get(
    "/sessions/{session_id}",
    response_model=ApiResponse[SessionView],
    summary="读取会话状态",
    description="按 session_id 恢复会话视图。他人会话一律 404。",
)
async def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    session = _get_owned_session(session_id, current_user, db)
    store = AgentSessionStore(db, current_user.id)
    return ApiResponse(
        success=True, data=_session_view(session, store), error=None,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ApiResponse[RunView],
    summary="运行一条用户消息",
)
async def post_message(
    session_id: str,
    req: RunMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    session = _get_owned_session(session_id, current_user, db)

    lock = _lock_for(session_id)
    if lock.locked():
        # 2.11：同一会话同一时刻只能有一个正在执行的用户消息
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该会话已有正在执行的消息，请等待完成后再发送",
        )

    async with lock:
        return ApiResponse(
            success=True,
            data=await _execute_run(session, req.message, db, current_user),
            error=None,
        )


@router.get(
    "/sessions/{session_id}/trace",
    response_model=ApiResponse[TraceView],
    summary="读取会话的运行轨迹",
)
async def get_trace(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    session = _get_owned_session(session_id, current_user, db)
    rows = AgentSessionStore(db, current_user.id).runs(session_id)
    return ApiResponse(
        success=True,
        data=TraceView(
            session_id=session.id,
            runs=[
                RunTrace(
                    run_id=r.id,
                    status=r.status,
                    decision=r.decision,
                    latency_ms=r.latency_ms or 0,
                    context_hash=r.context_hash,
                    tool_calls=[ToolCallView(**t) for t in (r.tool_calls or [])],
                    errors=list(r.errors or []),
                    created_at=r.created_at.isoformat() if r.created_at else "",
                )
                for r in rows
            ],
        ),
        error=None,
    )


@router.get(
    "/tools",
    response_model=ApiResponse[List[ToolView]],
    summary="列出 Agent 可用工具",
    description=(
        "从 ToolCatalog 读取，是工具定义的唯一来源。"
        "含写工具（``kind=write``）——但写工具**不会**出现在模型的 function-calling"
        "清单里，只能经确认流程执行。"
    ),
)
async def list_agent_tools(
    current_user: User = Depends(get_current_user_required),
):
    return ApiResponse(
        success=True,
        data=[
            ToolView(
                name=s.name, wire_name=s.wire_name, version=s.version, kind=s.kind,
                description=s.description, evidence_policy=s.evidence_policy,
                required_scopes=list(s.required_scopes),
                confirmation_required=s.confirmation_required,
                idempotency_required=s.idempotency_required,
            )
            for s in list_tools()
        ],
        error=None,
    )


# ===========================================================================
# 写操作确认
# ===========================================================================
@router.post(
    "/sessions/{session_id}/actions/{action_id}",
    response_model=ApiResponse[RunView],
    summary="确认或拒绝一个待执行的写操作",
    description=(
        "恢复被 ``interrupt()`` 暂停的运行。**只有这里**能让写操作真正执行。"
        "执行前会重新校验用户、意图状态、过期时间；对象是否被改动由 CAS 判断。"
    ),
)
async def decide_action(
    session_id: str,
    action_id: str,
    req: ActionDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    session = _get_owned_session(session_id, current_user, db)

    lock = _lock_for(session_id)
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该会话已有正在执行的消息，请等待完成后再确认",
        )

    async with lock:
        return ApiResponse(
            success=True,
            data=await _decide_action(session, action_id, req.approved, db, current_user),
            error=None,
        )


@router.get(
    "/sessions/{session_id}/actions",
    response_model=ApiResponse[List[ActionIntentView]],
    summary="列出该会话待处理的写操作",
)
async def list_pending_actions(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    session = _get_owned_session(session_id, current_user, db)
    rows = ActionIntentStore(db, current_user.id).list_pending(session_id)
    return ApiResponse(
        success=True,
        data=[
            ActionIntentView(
                action_id=r.action_id,
                tool=r.type,
                status=r.status,
                application_id=(r.arguments or {}).get("application_id"),
                expected_status=(r.arguments or {}).get("expected_status"),
                new_status=(r.arguments or {}).get("new_status"),
                reason=r.reason or "",
                expires_at=r.expires_at.isoformat() if r.expires_at else None,
            )
            for r in rows
        ],
        error=None,
    )


# ===========================================================================
# 长期记忆
# ===========================================================================
@router.get(
    "/memories",
    response_model=ApiResponse[List[MemoryView]],
    summary="列出我的长期记忆",
    description=(
        "默认不含 ``superseded``（被取代的记忆已不在召回面）。"
        "``proposed`` 是**待你确认**的推断；只有 ``confirmed`` 会被 Agent 召回。"
    ),
)
async def list_memories(
    status_filter: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    repo = MemoryRepository(db, current_user.id)
    rows = repo.list(
        status=status_filter, memory_type=memory_type,
        limit=max(1, min(int(limit or 100), 500)),
    )
    return ApiResponse(success=True, data=[_memory_view(r) for r in rows], error=None)


@router.post(
    "/memories",
    response_model=ApiResponse[MemoryView],
    summary="显式添加一条偏好",
    description=(
        "用户**亲口说的**偏好，因此直接落 ``confirmed`` / ``user_explicit``（8.2 节）。"
        "内容受结构约束：≤500 字、≤3 行、不得含凭据特征。"
    ),
)
async def create_memory(
    req: CreateMemoryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    service = MemoryService(MemoryRepository(db, current_user.id))
    try:
        row = service.add_user_preference(
            content=req.content, conflict_key=req.conflict_key,
        )
    except MemoryContentRejected as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return ApiResponse(success=True, data=_memory_view(row), error=None)


@router.post(
    "/memories/{memory_id}/confirm",
    response_model=ApiResponse[MemoryView],
    summary="确认一条待确认记忆（proposed → confirmed）",
    description=(
        "**只有用户能走这条路径**。确认后才进向量索引、才会被 Agent 召回；"
        "在此之前它不会被当成事实。"
    ),
)
async def confirm_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    repo = MemoryRepository(db, current_user.id)
    row = repo.get(memory_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")
    if row.status == "superseded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该记忆已被更新的偏好取代",
        )
    try:
        row = repo.confirm(memory_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return ApiResponse(success=True, data=_memory_view(row), error=None)


@router.delete(
    "/memories/{memory_id}",
    response_model=ApiResponse[Dict[str, Any]],
    summary="删除一条记忆",
    description="**同步删除结构化记录与向量索引**（8.2 / 15.2 节），不留残影。",
)
async def delete_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    repo = MemoryRepository(db, current_user.id)
    if not repo.delete(memory_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")
    return ApiResponse(success=True, data={"memory_id": memory_id, "deleted": True}, error=None)


# ===========================================================================
# 运行实现
# ===========================================================================
def _resolve_llm(user: User):
    """取本次运行要用的模型。**未配置或超额都返回明确错误，不静默降级。**"""
    try:
        return get_user_llm_or_raise(user)
    except QuotaExceededError as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("获取模型失败：%s", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="模型不可用，请先完成模型设置",
        )


async def _execute_run(session, message: str, db: Session, user: User) -> RunView:
    store = AgentSessionStore(db, user.id)
    run_id = uuid.uuid4().hex          # 21.6：一条用户消息 = 一个独立 run_id
    context_refs = refs_from_json(session.context_refs)

    # ---- 上下文预检查（零 LLM 调用）---------------------------------------
    built = ContextBuilder(db, user.id).build(
        context_refs=context_refs,
        entry_source=session.entry_source,
    )
    if built.missing_context:
        view = RunView(
            session_id=session.id,
            run_id=run_id,
            status="needs_input",
            needs_input=True,
            output=OutputView(
                decision=None, confidence=0.0,
                summary="上下文不完整，请先补齐目标 JD / 简历 / 投递记录。",
                missing_info=[m.hint or f"缺少 {m.type}" for m in built.missing_context],
            ),
            missing_context=list(built.missing_context),
            tool_calls=[],
            context_hash=built.context_hash,
            latency_ms=0,
            errors=[],
            checkpointer=checkpointer_status()["kind"],
        )
        store.record_run(session, {
            "run_id": run_id, "status": view.status, "decision": None,
            "latency_ms": 0, "context_hash": built.context_hash,
            "tool_calls": [], "errors": [], "created_at": _now_iso(),
        })
        return view

    # ---- 正式运行 ------------------------------------------------------
    llm = _resolve_llm(user)      # 失败会抛 HTTPException（不静默降级）
    deps = make_deps(db=db, user_id=user.id, llm=llm)

    state = new_agent_state(
        thread_id=session.id,              # session_id == thread_id（21.6）
        run_id=run_id,
        user_id=user.id,
        limits=LimitsState(),
    )
    state["input"].message = message
    state["input"].entry_source = session.entry_source
    state["input"].context_refs = list(context_refs)

    config = graph_config(session.id, deps)
    prev_errors = await _count_existing_errors(config)

    started = time.monotonic()
    final = await _invoke_graph(state, config, session, run_id, built.context_hash)
    if isinstance(final, RunView):        # 超时/异常已被转成视图
        store.record_run(session, {
            "run_id": run_id, "status": final.status, "decision": None,
            "latency_ms": final.latency_ms, "context_hash": built.context_hash,
            "tool_calls": [], "errors": list(final.errors), "created_at": _now_iso(),
        })
        return final

    latency_ms = int((time.monotonic() - started) * 1000)
    view = _build_run_view(
        session, final, run_id,
        fallback_hash=built.context_hash,
        latency_ms=latency_ms,
        error_offset=prev_errors,
    )
    _persist_run(store, session, view, latency_ms)
    logger.info(
        "Agent 运行完成：session=%s run=%s status=%s decision=%s tools=%d pending=%s",
        session.id, run_id, view.status, view.output.decision,
        len(view.tool_calls), bool(view.pending_action),
    )
    return view


async def _invoke_graph(
    state: Dict[str, Any], config: Dict[str, Any], session, run_id: str,
    context_hash: Optional[str],
):
    """跑图并把超时/异常转成 ``RunView``。``interrupt`` 暂停**不是**异常。"""
    try:
        return await asyncio.wait_for(
            get_agent_graph().ainvoke(state, config=config),
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Agent 运行超时：session=%s run=%s", session.id, run_id)
        return RunView(
            session_id=session.id, run_id=run_id, status="timeout",
            output=OutputView(summary=f"运行超过 {RUN_TIMEOUT_SECONDS} 秒已中止，可重试。"),
            context_hash=context_hash,
            latency_ms=RUN_TIMEOUT_SECONDS * 1000,
            errors=["agent_timeout"],
            checkpointer=checkpointer_status()["kind"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Agent 运行异常：session=%s run=%s", session.id, run_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent 运行失败，请稍后重试",
        ) from e


def _persist_run(store: AgentSessionStore, session, view: RunView, latency_ms: int) -> None:
    if view.status != "needs_input":
        store.set_last_output(session, view.output.model_dump())
    store.record_run(session, {
        "run_id": view.run_id,
        "status": view.status,
        "decision": view.output.decision,
        "latency_ms": latency_ms,
        "context_hash": view.context_hash,
        "tool_calls": [t.model_dump() for t in view.tool_calls],
        "errors": list(view.errors),
        "created_at": _now_iso(),
    })


async def _decide_action(
    session, action_id: str, approved: bool, db: Session, user: User,
) -> RunView:
    """恢复被 interrupt 暂停的运行。"""
    store = AgentSessionStore(db, user.id)
    intents = ActionIntentStore(db, user.id)
    row = intents.get(action_id)

    # 跨用户/不存在统一 404；会话不匹配也 404（不能拿别人的意图操作自己的会话）
    if row is None or row.session_id != session.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="待确认操作不存在",
        )

    check = check_resume_allowed(row, user_id=user.id)
    if not check.ok:
        if check.code == "action_expired":
            intents.mark_expired(action_id)
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="该确认已过期，请让 Agent 重新提出建议",
            )
        if check.code.startswith("action_already_"):
            # 幂等：已经处理过——返回该运行的最新视图而不是报错
            return _view_from_stored_run(session, store, row.run_id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=check.message)

    # resume 之后只会跑 execute_write_action → finalize → persist_outcome，
    # **没有模型调用**，因此不需要 LLM（也不需要校验配额）。
    deps = make_deps(db=db, user_id=user.id, llm=None)
    config = graph_config(session.id, deps)
    prev_errors = await _count_existing_errors(config)

    from langgraph.types import Command

    started = time.monotonic()
    try:
        final = await asyncio.wait_for(
            get_agent_graph().ainvoke(Command(resume={"approved": approved}), config=config),
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("确认写操作超时：session=%s action=%s", session.id, action_id)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="确认处理超时，请重试",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("确认写操作失败：session=%s action=%s", session.id, action_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="操作失败，请稍后重试",
        ) from e

    latency_ms = int((time.monotonic() - started) * 1000)
    view = _build_run_view(
        session, final, row.run_id,
        fallback_hash=getattr(session, "context_hash", None),
        latency_ms=latency_ms,
        error_offset=prev_errors,
    )
    _persist_run(store, session, view, latency_ms)
    logger.info(
        "写操作裁决完成：session=%s action=%s approved=%s status=%s wrote=%s",
        session.id, action_id, approved, view.status, view.wrote_business_data,
    )
    return view


def _view_from_stored_run(session, store: AgentSessionStore, run_id: str) -> RunView:
    """幂等路径：把已存下的运行记录还原成 ``RunView``。"""
    for row in store.runs(session.id, limit=200):
        if row.id != run_id:
            continue
        output = OutputView(
            decision=row.decision,
            summary=(session.last_output or {}).get("summary", "") if session.last_output else "",
        )
        return RunView(
            session_id=session.id, run_id=run_id, status=row.status,
            output=output,
            tool_calls=[ToolCallView(**t) for t in (row.tool_calls or [])],
            context_hash=row.context_hash,
            latency_ms=row.latency_ms or 0,
            errors=list(row.errors or []),
            checkpointer=checkpointer_status()["kind"],
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="运行记录不存在")


async def _count_existing_errors(config: Dict[str, Any]) -> int:
    """读该 thread 已有的错误条数（用于把本次运行的错误切出来）。

    ⚠️ 为什么只有 ``errors`` 需要切片：``errors`` 的 reducer 是 ``operator.add``
    （12.4 节，保留全部历史），同一个 thread 的第二次运行会带着第一次的错误一起
    返回；而 ``tool_loop``、``decision``、``output`` 都是普通通道，每轮被输入状态
    整体替换，天然只含本次运行。所以 ``RunView`` 里两处取值的做法**故意不同**。
    """
    try:
        snapshot = await get_agent_graph().aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        return len(values.get("errors") or [])
    except Exception:  # noqa: BLE001
        logger.debug("读取已有错误条数失败，按 0 处理", exc_info=True)
        return 0


def _build_run_view(
    session,
    final: Dict[str, Any],
    run_id: str,
    *,
    fallback_hash: Optional[str],
    latency_ms: int,
    error_offset: int = 0,
) -> RunView:
    observability = final.get("observability")
    run_state = final.get("run")
    all_errors = list(final.get("errors") or [])
    run_status = getattr(run_state, "status", "failed")

    # ``pending_action`` 只在"正等确认"时有意义（见 ActionIntentView 的说明）。
    # 写完之后它就成了历史——继续返回会让前端以为还有一个待确认项。
    pending = (
        _pending_action_view(final) if run_status == "waiting_confirmation" else None
    )

    return RunView(
        session_id=session.id,
        run_id=run_id,
        status=run_status,
        needs_input=False,
        output=_output_view(final),
        missing_context=list(final["context"].missing_context) if final.get("context") else [],
        tool_calls=_tool_call_views(final),
        context_hash=getattr(observability, "context_hash", None) or fallback_hash,
        latency_ms=latency_ms,
        errors=[e.message for e in all_errors[error_offset:]],
        pending_action=pending,
        wrote_business_data=_did_write_business_data(final),
        write_failure=_write_failure_view(final),
        checkpointer=checkpointer_status()["kind"],
    )


def _write_failure_view(final: Dict[str, Any]) -> Optional[WriteFailureView]:
    """「用户确认了，但没写成」——判据同样是**状态机**，不是文案。

    三个条件同时成立才算数：
    1. ``approval.decision == approved``：用户确实点了确认（拒绝/取消不算）；
    2. ``pending_action`` 存在且**没**落 ``succeeded``；
    3. 能找到一条 ``tool_name`` 等于该意图类型的错误——写失败的错误是
       ``execute_write_action`` 用 ``row.type`` 标记的（graph.py），而模型调用
       失败、输出校验失败这些错误**不带 tool_name**，因此不会误判。

    刻意不复用 ``_did_write_business_data`` 的反面：那个函数对"没走写路径"
    和"走写路径但失败"都返回 False，这里要区分二者。
    """
    approval = final.get("approval")
    if approval is None:
        return None
    if getattr(approval, "decision", None) != "approved":
        return None
    pending = getattr(approval, "pending_action", None)
    if pending is None:
        return None
    if getattr(pending, "status", "") == "succeeded":
        return None
    for err in final.get("errors") or []:
        if getattr(err, "tool_name", None) == pending.type:
            return WriteFailureView(
                code=getattr(err, "code", "") or "write_failed",
                message=getattr(err, "message", "") or "写操作未成功",
                tool=pending.type,
            )
    return None


def _did_write_business_data(final: Dict[str, Any]) -> bool:
    """本轮是否真的写了业务数据。

    判据是**状态机**而不是"有没有调过写工具"：``approval.decision == approved``
    且意图已落 ``succeeded``。``execute_write_action`` 只在用户确认后才置位，
    所以这一条等价于「用户确认过且执行成功」。
    """
    approval = final.get("approval")
    if approval is None:
        return False
    if getattr(approval, "decision", None) != "approved":
        return False
    pending = getattr(approval, "pending_action", None)
    if pending is None:
        return False
    return getattr(pending, "status", "") == "succeeded"
