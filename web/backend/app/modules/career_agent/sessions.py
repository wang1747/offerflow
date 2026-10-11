"""Agent 会话与运行的仓储（v0.2-beta）。

替代 alpha 的**进程内 ``OrderedDict``**。换掉它的直接原因见
``app/models/agent_session.py`` 的文件头：Checkpointer 一旦持久化，
"注册表在内存、断点在数据库"的不对称就成了一个会 404 的缺陷。

这里刻意保持 alpha 的三条语义不变：
- ``session_id == thread_id``（21.6）；
- **他人会话与不存在的会话返回同一个结果**（跨用户统一 404）；
- 同一上下文哈希复用会话（2.6：不重复创建）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.agent_session import AgentRun, AgentSession
from app.modules.career_agent.context import ContextBuildResult
from app.modules.career_agent.state import ContextRef

logger = logging.getLogger(__name__)

#: 单会话 Trace 返回的最大运行条数（有界：Trace 是排查用的，不是账本）
MAX_RUNS_PER_SESSION = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refs_to_json(refs: List[ContextRef]) -> List[Dict[str, Any]]:
    return [r.model_dump() for r in refs]


def refs_from_json(rows: Any) -> List[ContextRef]:
    """把 DB 里的 JSON 还原成 ``ContextRef``。

    逐条构造而不是直接 ``ContextRef(**row)``：历史行可能是旧格式
    （少字段、多字段），一条坏数据不该让整个会话读不出来。
    """
    out: List[ContextRef] = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        try:
            out.append(ContextRef(**row))
        except Exception:  # noqa: BLE001
            logger.warning("会话上下文引用解析失败，已跳过：%r", row)
    return out


class AgentSessionStore:
    """``agent_sessions`` / ``agent_runs`` 的读写。``user_id`` 运行时注入。"""

    def __init__(self, db, user_id: str) -> None:
        self._db = db
        self._user_id = user_id

    # ------------------------------------------------------------------
    def create(
        self,
        *,
        session_id: str,
        entry_source: str,
        context_refs: List[ContextRef],
        built: ContextBuildResult,
        warnings: Optional[List[str]] = None,
    ) -> AgentSession:
        session = AgentSession(
            id=session_id,
            user_id=self._user_id,
            entry_source=entry_source,
            context_refs=_refs_to_json(context_refs),
            context_hash=built.context_hash,
            context_version=built.context.context_version,
            missing_context=[m.model_dump() for m in built.missing_context],
            warnings=[w.message for w in built.warnings] + list(warnings or []),
            status="created",
            run_count=0,
        )
        self._db.add(session)
        self._db.commit()
        self._db.refresh(session)
        return session

    def get(self, session_id: str) -> Optional[AgentSession]:
        """**按 user_id 过滤**——他人会话与不存在返回同一个 None。"""
        return (
            self._db.query(AgentSession)
            .filter(
                AgentSession.id == session_id,
                AgentSession.user_id == self._user_id,
            )
            .first()
        )

    def find_by_context_hash(self, context_hash: Optional[str]) -> Optional[AgentSession]:
        """同一用户 + 同一上下文哈希 ⇒ 复用最近一条（2.6 节）。"""
        if not context_hash:
            return None
        return (
            self._db.query(AgentSession)
            .filter(
                AgentSession.user_id == self._user_id,
                AgentSession.context_hash == context_hash,
            )
            .order_by(AgentSession.created_at.desc())
            .first()
        )

    # ------------------------------------------------------------------
    def record_run(
        self,
        session: AgentSession,
        run: Dict[str, Any],
        *,
        run_index: Optional[int] = None,
    ) -> AgentRun:
        """写入或**更新**一条运行记录（按 ``run_id`` upsert）。

        为什么要 upsert：写路径的运行会被 ``interrupt`` 分成两段——
        暂停时先记一条 ``waiting_confirmation``，用户确认后同一个 ``run_id``
        跑完再更新成终态。若只会 insert，Trace 里就会出现两条同 id 的记录。
        """
        run_id = run["run_id"]
        row = (
            self._db.query(AgentRun)
            .filter(AgentRun.id == run_id, AgentRun.user_id == self._user_id)
            .first()
        )
        if row is None:
            row = AgentRun(
                id=run_id,
                session_id=session.id,
                user_id=self._user_id,
                run_index=run_index if run_index is not None else (session.run_count + 1),
            )
            self._db.add(row)
            session.run_count = (session.run_count or 0) + 1

        row.status = run.get("status") or row.status
        row.decision = run.get("decision")
        row.latency_ms = int(run.get("latency_ms") or 0)
        row.context_hash = run.get("context_hash")
        row.tool_calls = list(run.get("tool_calls") or [])
        row.errors = list(run.get("errors") or [])
        session.status = row.status
        session.updated_at = datetime.now(timezone.utc)
        self._db.commit()
        self._db.refresh(row)
        return row

    def set_last_output(self, session: AgentSession, output: Dict[str, Any]) -> None:
        session.last_output = dict(output or {})
        self._db.commit()

    def runs(self, session_id: str, limit: int = MAX_RUNS_PER_SESSION) -> List[AgentRun]:
        return (
            self._db.query(AgentRun)
            .filter(AgentRun.session_id == session_id, AgentRun.user_id == self._user_id)
            .order_by(AgentRun.run_index.asc())
            .limit(limit)
            .all()
        )

    def pending_run(self, session_id: str) -> Optional[AgentRun]:
        """该会话里停在 ``waiting_confirmation`` 的那一条运行（若有）。

        API 用它判断"这个会话当前是否有一个待确认的写操作"——
        并据此决定重复确认是 409 还是幂等成功。
        """
        return (
            self._db.query(AgentRun)
            .filter(
                AgentRun.session_id == session_id,
                AgentRun.user_id == self._user_id,
                AgentRun.status == "waiting_confirmation",
            )
            .order_by(AgentRun.run_index.desc())
            .first()
        )
