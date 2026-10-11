"""写操作策略层：``ActionIntent`` 的生成、持久化与「恢复前重校验」。

设计依据：设计文档 9.2（写工具规则）、10（ActionIntent 契约）、21.5（interrupt 选择）。

## 三个必须做对的地方

**① 幂等键必须是确定性的，不能是随机 uuid。**
LangGraph 的 ``interrupt()`` 在恢复时会**从节点开头重放**——也就是说
``prepare_write_action`` 里"创建意图"那段会跑第二遍。用随机键会造出第二条意图，
用户会看到两张确认卡。这里用 ``sha256(session|run|type|args)`` 派生，
重放天然命中同一行（配合 ``agent_action_intents`` 的唯一约束）。

**② 恢复前必须重校验**（21.5 节原话）。
用户可能几分钟后才点确认，期间容器可能重启、权限可能变化、对象可能被改。
``check_resume_allowed`` 把这几条写成**可单测的返回值**而不是散落在节点里的 if。

**③ 业务版本只能来自 capability。**
``mark_succeeded`` 的 ``result`` 参数只接受 capability 的返回值；
Agent 不得自行构造"成功"。10 节的最后一句就是这个意思。

## 幂等为什么放在这张表而不是业务表

业务表（``applications`` / ``application_events``）不该为 Agent 的机制加列。
``agent_action_intents`` 记录的是「意图与执行事实」，不是业务状态，
因此与「Agent 不存第二份业务真值」并不冲突——业务真值仍然只在 applications 里。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.models.agent_action import ACTION_TERMINAL_STATUSES, AgentActionIntent
from app.modules.career_agent.state import ActionIntent

logger = logging.getLogger(__name__)

#: 确认窗口。超时后意图作废——用户点开一张 15 分钟前的确认卡时，
#: 卡片描述的状态很可能已经变了，此时"重校验再执行"不如"要求重新提议"干净。
INTENT_TTL_MINUTES = 15

#: ``ActionCandidate.type`` → 契约工具名。**白名单**：只有列在这里的行动才可能被
#: 转成 ActionIntent。模型产出的 ``type`` 直接决定能不能走写路径，
#: 所以这里必须是一个闭集合（否则等于让模型自选工具）。
ACTION_TOOL_MAP: Dict[str, str] = {
    "update_application_status": "application.update_status",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def action_tool_for(action_type: str) -> Optional[str]:
    """把建议行动的 ``type`` 映射成写工具契约名；不在白名单返回 None。"""
    return ACTION_TOOL_MAP.get((action_type or "").strip())


def new_idempotency_key(
    session_id: str, run_id: str, tool_name: str, arguments: Dict[str, Any],
) -> str:
    """确定性幂等键（见文件头 ①）。

    ``sort_keys=True``：参数顺序不同但内容相同的两次提议必须得到同一个键，
    否则重放时键会漂移，幂等失效。
    """
    material = "|".join([
        session_id or "", run_id or "", tool_name or "",
        json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def new_action_id(idempotency_key: str) -> str:
    """从幂等键派生 action_id——保证重放时是**同一个** action_id。"""
    return "act_" + (idempotency_key or uuid.uuid4().hex)[:24]


# ===========================================================================
# 恢复前重校验（21.5）
# ===========================================================================
@dataclass
class ResumeCheck:
    """恢复前的检查结果。``ok=False`` 时由调用方把 ``code`` 写进 errors/状态。"""

    ok: bool
    code: str = ""
    message: str = ""

    @staticmethod
    def passed() -> "ResumeCheck":
        return ResumeCheck(ok=True)

    @staticmethod
    def rejected(code: str, message: str) -> "ResumeCheck":
        return ResumeCheck(ok=False, code=code, message=message)


def check_resume_allowed(
    row: Optional[AgentActionIntent],
    *,
    user_id: str,
    now: Optional[datetime] = None,
) -> ResumeCheck:
    """用户点「确认」之后、真正执行写操作之前，重新检查一遍。

    覆盖 21.5 要求的四项：

    - **用户**：意图必须属于当前登录用户（跨用户确认直接拒绝）；
    - **对象/版本**：由 capability 的 CAS 承担（这里只保证意图未过期）；
    - **权限**：意图的 ``type`` 必须在写工具白名单里；
    - **幂等键**：终态（``succeeded``/``failed``/``expired``/``cancelled``）
      不再重复执行——重复确认只会拿到上次结果。
    """
    moment = now or _now()
    if row is None:
        return ResumeCheck.rejected("action_not_found", "该待确认操作不存在")
    if row.user_id != user_id:
        # 与 capabilities 层同一策略：不区分"不存在"与"无权限"
        return ResumeCheck.rejected("action_not_found", "该待确认操作不存在")

    # 意图里的 ``type`` 是**契约工具名**（``application.update_status``）。
    # 必须能在 ToolCatalog 里查到一个 ``kind="write"`` 的工具——
    # 以目录为唯一真相，而不是在这里再维护一份白名单。
    from app.modules.career_agent.catalog import CATALOG

    spec = CATALOG.get(row.type or "")
    if spec is None or spec.kind != "write":
        return ResumeCheck.rejected("action_not_allowed", "该操作不在允许的写入白名单内")

    if row.status in ACTION_TERMINAL_STATUSES:
        return ResumeCheck.rejected(
            f"action_already_{row.status}", f"该操作已结束（{row.status}），不再重复执行",
        )
    if row.expires_at is not None:
        expires = row.expires_at
        if expires.tzinfo is None:
            # SQLite 存回来可能丢时区，按 UTC 解释（与写入时一致）
            expires = expires.replace(tzinfo=timezone.utc)
        if moment > expires:
            return ResumeCheck.rejected(
                "action_expired", "该确认已过期，请让 Agent 重新提出建议",
            )
    return ResumeCheck.passed()


# ===========================================================================
# 意图持久化
# ===========================================================================
class ActionIntentStore:
    """``agent_action_intents`` 的读写。``user_id`` 运行时注入，不来自模型。"""

    def __init__(self, db, user_id: str) -> None:
        self._db = db
        self._user_id = user_id

    def upsert_pending(
        self,
        intent: ActionIntent,
        *,
        session_id: str,
        run_id: str,
    ) -> AgentActionIntent:
        """创建待确认意图；**重放时返回已有行**（幂等）。

        路径上会先按 ``(user_id, idempotency_key)`` 查一次。并发下的兜底是表上的
        唯一约束——这里不 catch ``IntegrityError`` 后重查，是因为图的同一 thread
        由 API 层的会话锁串行化（``routes._lock_for``），不存在并发重放。
        """
        assert intent.idempotency_key, "ActionIntent 必须有幂等键"
        existing = self.get_by_idempotency_key(intent.idempotency_key)
        if existing is not None:
            return existing

        row = AgentActionIntent(
            action_id=intent.action_id,
            user_id=self._user_id,
            session_id=session_id,
            run_id=run_id,
            type=intent.type,
            arguments=dict(intent.arguments or {}),
            reason=intent.reason or "",
            status="pending_confirmation",
            source_context_version=intent.source_context_version,
            expected_version=intent.expected_version,
            idempotency_key=intent.idempotency_key,
            expires_at=_now() + timedelta(minutes=INTENT_TTL_MINUTES),
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def get(self, action_id: str) -> Optional[AgentActionIntent]:
        return (
            self._db.query(AgentActionIntent)
            .filter(
                AgentActionIntent.action_id == action_id,
                AgentActionIntent.user_id == self._user_id,
            )
            .first()
        )

    def get_by_idempotency_key(self, key: str) -> Optional[AgentActionIntent]:
        return (
            self._db.query(AgentActionIntent)
            .filter(
                AgentActionIntent.idempotency_key == key,
                AgentActionIntent.user_id == self._user_id,
            )
            .first()
        )

    def mark_confirmed(self, action_id: str) -> Optional[AgentActionIntent]:
        row = self.get(action_id)
        if row is None:
            return None
        row.status = "confirmed"
        row.decided_at = _now()
        self._db.commit()
        self._db.refresh(row)
        return row

    def mark_cancelled(self, action_id: str) -> Optional[AgentActionIntent]:
        row = self.get(action_id)
        if row is None:
            return None
        row.status = "cancelled"
        row.decided_at = _now()
        self._db.commit()
        self._db.refresh(row)
        return row

    def mark_expired(self, action_id: str) -> Optional[AgentActionIntent]:
        row = self.get(action_id)
        if row is None:
            return None
        row.status = "expired"
        self._db.commit()
        self._db.refresh(row)
        return row

    def mark_succeeded(self, action_id: str, result: Dict[str, Any]) -> Optional[AgentActionIntent]:
        """记录执行成功。``result`` **必须是 capability 的返回值**（见文件头 ③）。"""
        row = self.get(action_id)
        if row is None:
            return None
        row.status = "succeeded"
        row.result = dict(result or {})
        row.executed_at = _now()
        self._db.commit()
        self._db.refresh(row)
        return row

    def mark_failed(self, action_id: str, error_code: str) -> Optional[AgentActionIntent]:
        row = self.get(action_id)
        if row is None:
            return None
        row.status = "failed"
        row.error_code = error_code
        row.executed_at = _now()
        self._db.commit()
        self._db.refresh(row)
        return row

    def list_pending(self, session_id: Optional[str] = None) -> list:
        query = self._db.query(AgentActionIntent).filter(
            AgentActionIntent.user_id == self._user_id,
            AgentActionIntent.status.in_(("pending_confirmation", "confirmed")),
        )
        if session_id:
            query = query.filter(AgentActionIntent.session_id == session_id)
        return query.order_by(AgentActionIntent.created_at.desc()).all()


def row_to_state_intent(row: AgentActionIntent) -> ActionIntent:
    """DB 行 → State 里的 ``ActionIntent``（用于把它写进 ``state["approval"]``）。"""
    return ActionIntent(
        action_id=row.action_id,
        type=row.type,
        arguments=dict(row.arguments or {}),
        reason=row.reason or "",
        status=row.status,
        source_context_version=row.source_context_version,
        expected_version=None,
        idempotency_key=row.idempotency_key,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
    )
