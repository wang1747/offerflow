"""v0.2-beta 验收：PostgresSaver、会话落库、长期记忆、确认写入。

与 ``test_agent_api.py`` 的分工：那里守 alpha 的只读切片（仍然全部通过），
这里守 beta 新增的四件事。**每条断言都对着一种具体的失败形态写**，
并在关键处给出"把缺陷放回去会变红"的理由。

三条 beta 的硬红线，每条都有正反两面：

| 红线 | 正面证据 | 反面（放回缺陷会红） |
|---|---|---|
| 确认前绝不写业务数据 | 提议后状态与 events 都不变 | 若在 prepare 里就执行，E1 变红 |
| 跨用户 0 泄漏 | 他人会话/意图/记忆全 404 或空 | 若去掉 user_id 过滤，C6/E7 变红 |
| 幂等键真的生效 | 重复确认只写一次 | 若用随机键，E5 会写出第二条事件 |
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.models.agent_action import AgentActionIntent
from app.models.agent_memory import AgentMemory
from app.models.agent_session import AgentRun, AgentSession
from app.models.application import Application, ApplicationEvent
from app.models.jd import JobDescription
from app.models.resume import Resume
from app.models.user import User
from app.modules.auth.routes import get_current_user_required
from app.modules.career_agent import routes as agent_routes
from app.modules.career_agent.checkpoint import (
    CHECKPOINTER_MEMORY,
    is_postgres_url,
    open_checkpointer,
    to_psycopg_dsn,
)
from app.modules.career_agent.memory import MemoryService
from app.modules.career_agent.memory_repository import (
    MemoryContentRejected,
    MemoryRepository,
    _reset_collection_cache_for_tests,
    get_memory_collection,
)
from app.modules.career_agent.policies import (
    action_tool_for,
    check_resume_allowed,
    new_idempotency_key,
)
from app.modules.career_agent.sessions import AgentSessionStore

BASE = "/api/v1/agent"


# ===========================================================================
# A. Checkpointer：DSN 转换是**必要**的（不是风格问题）
# ===========================================================================
def test_sqlalchemy_url_is_converted_to_psycopg_dsn():
    """SQLAlchemy 的 ``postgresql+psycopg://`` 必须剥掉方言后缀。"""
    out = to_psycopg_dsn("postgresql+psycopg://u:p@h:5432/db")
    assert out == "postgresql://u:p@h:5432/db"
    assert "+" not in out.split("://", 1)[0]


def test_psycopg_rejects_the_unconverted_url():
    """**反向对照**：不转换的话 psycopg 根本解析不了这个 URL。

    这条是上面那个转换函数的"必要性证明"。没有它，"转换函数返回了正确字符串"
    只证明了它自己自洽；有了它，才证明**不转换会真的坏**。
    """
    from psycopg.conninfo import conninfo_to_dict

    raw = "postgresql+psycopg://u:p@h:5432/db"
    with pytest.raises(Exception) as exc:
        conninfo_to_dict(raw)
    assert "missing" in str(exc.value).lower() or "invalid" in str(exc.value).lower()

    # 转换之后必须能正常解析（否则等于换了个坏东西）
    parsed = conninfo_to_dict(to_psycopg_dsn(raw))
    assert parsed["dbname"] == "db" and parsed["host"] == "h"


def test_to_psycopg_dsn_leaves_non_postgres_urls_alone():
    for url in ("sqlite:///./x.db", "", "mysql+pymysql://u@h/db"):
        assert to_psycopg_dsn(url) == url


def test_is_postgres_url():
    assert is_postgres_url("postgresql+psycopg://u@h/db")
    assert is_postgres_url("postgres://u@h/db")
    assert not is_postgres_url("sqlite:///x.db")
    assert not is_postgres_url("")


@pytest.mark.asyncio
async def test_sqlite_database_falls_back_to_memory_checkpointer():
    """非 Postgres 环境**明确**退回内存 saver，并把类型报出来（不静默）。

    ``kind="memory"`` 会被写进每个会话响应（``SessionView.checkpointer``）——
    "没有持久化"这件事因此永远看得见，而不是等到重启后会话消失才发现。
    """
    handle = await open_checkpointer("sqlite:///./whatever.db")
    try:
        assert handle.kind == CHECKPOINTER_MEMORY
        assert handle.is_persistent is False
    finally:
        await handle.aclose()


# ===========================================================================
# B. 会话与运行落库（alpha 是进程内 dict）
# ===========================================================================
@pytest.fixture
def beta_env(tmp_path):
    """带 JD / 简历 / **投递记录** 的 TestClient；身份可在 u1/u2 间切换。"""
    from app.config import get_settings

    get_settings().chroma_persist_path = str(tmp_path / "chroma")

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'beta.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as s:
        s.add(User(id="u1", name="甲"))
        s.add(User(id="u2", name="乙"))
        s.add(JobDescription(
            id="jd1", user_id="u1",
            raw_text="Python 后端开发，需要 Redis 与高并发经验。",
            company="A公司", position="后端工程师", must_have=["Python", "Redis"],
        ))
        s.add(Resume(id="r1", user_id="u1", version=1, raw_text="张三 后端工程师 " * 40))
        s.add(Application(
            id="app1", user_id="u1", company="A公司",
            position="后端工程师", status="saved",
        ))
        s.add(Application(
            id="app2", user_id="u2", company="乙公司",
            position="Rust 工程师", status="saved",
        ))
        s.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    holder = {"user": User(id="u1", name="甲")}

    def override_current_user(request: Request):
        uid = request.headers.get("x-test-user")
        return User(id=uid, name=uid) if uid else holder["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_required] = override_current_user
    agent_routes._reset_agent_api_state()
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, holder=holder, Session=Session)
    app.dependency_overrides.clear()
    agent_routes._reset_agent_api_state()
    engine.dispose()


@pytest.fixture
def memory_env(tmp_path):
    """内存 SQLite + 独立 Chroma 目录，用于记忆仓储的直测。"""
    from app.config import get_settings

    get_settings().chroma_persist_path = str(tmp_path / "chroma")
    _reset_collection_cache_for_tests()

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    db.add(User(id="u1", name="甲"))
    db.add(User(id="u2", name="乙"))
    db.commit()
    try:
        yield SimpleNamespace(db=db, Session=Session)
    finally:
        db.close()
        engine.dispose()
        _reset_collection_cache_for_tests()


def _create(env, **overrides):
    body = {"entry_source": "jd_page", "jd_id": "jd1", "resume_id": "r1"}
    body.update(overrides)
    return env.client.post(f"{BASE}/sessions", json=body)


def _run(env, session_id, message="我下一步该做什么？"):
    return env.client.post(
        f"{BASE}/sessions/{session_id}/messages", json={"message": message},
    )


def _refs_from(messages):
    """只从 Human 轮取 ``available_evidence_refs``（System 提示词里也有这个词）。"""
    for m in messages:
        if not isinstance(m, HumanMessage):
            continue
        content = getattr(m, "content", "") or ""
        if "available_evidence_refs" in content:
            raw = content[content.find("{"): content.rfind("}") + 1]
            return json.loads(raw).get("available_evidence_refs") or []
    return []


def _use_llm(monkeypatch, llm_factory):
    monkeypatch.setattr(
        agent_routes, "get_user_llm_or_raise", lambda user: llm_factory(),
    )


class ReadOnlyLLM:
    """选 jd/resume/application 三个只读工具，给一个普通结论（不提写行动）。"""

    def __init__(self):
        self._selecting = False

    def bind_tools(self, tools, **kwargs):
        self._selecting = True
        return self

    def invoke(self, messages):
        if self._selecting:
            self._selecting = False
            return AIMessage(content="", tool_calls=[
                {"name": "jd_get", "args": {"jd_id": "jd1"}, "id": "c1", "type": "tool_call"},
                {"name": "resume_get", "args": {"resume_id": "r1"}, "id": "c2", "type": "tool_call"},
                {"name": "application_get", "args": {"application_id": "app1"},
                 "id": "c3", "type": "tool_call"},
            ])
        refs = _refs_from(messages)
        return AIMessage(content=json.dumps({
            "decision": "optimize_first",
            "confidence": 0.8,
            "summary": "建议先补 Redis 证据再投。",
            "actions": [{
                "type": "optimize_resume", "title": "补充 Redis 证据",
                "priority": 1, "reason": "JD 把 Redis 列为必备。",
                "evidence_refs": refs[:1], "requires_confirmation": True,
            }],
            "evidence": [{"ref": r, "source": "jd.get"} for r in refs],
            "missing_info": [],
        }, ensure_ascii=False))


class WriteIntentLLM(ReadOnlyLLM):
    """在结论里提议一个**投递状态变更**——这是 beta 写路径的入口。

    ``new_status`` 可参数化：用来构造"合法提议"与"非法目标状态"两种场景。
    """

    def __init__(self, new_status="applied", application_id="app1"):
        super().__init__()
        self._new_status = new_status
        self._application_id = application_id

    def invoke(self, messages):
        if self._selecting:
            return super().invoke(messages)
        refs = _refs_from(messages)
        return AIMessage(content=json.dumps({
            "decision": "apply_now",
            "confidence": 0.9,
            "summary": "已投递，建议把状态推进为 applied。",
            "actions": [{
                "type": "update_application_status",
                "title": "把投递状态更新为已投递",
                "priority": 1,
                "reason": "我已经投递了这个岗位。",
                "evidence_refs": refs[:1],
                "requires_confirmation": True,
                "arguments": {"new_status": self._new_status},
            }],
            "evidence": [{"ref": r, "source": "jd.get"} for r in refs],
            "missing_info": [],
        }, ensure_ascii=False))


def test_session_and_runs_are_persisted(beta_env, monkeypatch):
    """会话与运行必须**落库**——这是换 PostgresSaver 后的必要配套。

    改这条前后可以这样看：alpha 的会话只在进程内，重启即失；现在同一个
    ``agent_sessions`` 行在多次请求之间、以及新开的 Session 里都能读到。
    """
    _use_llm(monkeypatch, ReadOnlyLLM)
    session_id = _create(beta_env).json()["data"]["session_id"]
    _run(beta_env, session_id)
    _run(beta_env, session_id)

    with beta_env.Session() as s:
        row = s.query(AgentSession).filter(AgentSession.id == session_id).first()
        assert row is not None, "会话必须落库（alpha 的进程内注册表已被替换）"
        assert row.user_id == "u1"
        assert row.id == session_id, "21.6：session_id == thread_id"
        assert row.run_count == 2

        runs = (
            s.query(AgentRun).filter(AgentRun.session_id == session_id)
            .order_by(AgentRun.run_index.asc()).all()
        )
        assert [r.run_index for r in runs] == [1, 2]
        assert all(r.status == "completed" for r in runs)
        # 运行索引里**不得**出现工具原始返回或正文（只存视图级信息）
        assert all("result_summary" not in (t or {}) for r in runs for t in (r.tool_calls or []))


def test_trace_reads_from_database(beta_env, monkeypatch):
    """Trace 来自 ``agent_runs``，不是内存列表。"""
    _use_llm(monkeypatch, ReadOnlyLLM)
    session_id = _create(beta_env).json()["data"]["session_id"]
    _run(beta_env, session_id)

    data = beta_env.client.get(f"{BASE}/sessions/{session_id}/trace").json()["data"]
    assert len(data["runs"]) == 1
    assert data["runs"][0]["status"] == "completed"


def test_store_is_tenant_scoped(memory_env):
    """仓储层也必须按 user_id 过滤——不能只在 API 层过滤。

    只在 API 层过滤是常见写法，但只要有任何一处直接调用仓储（脚本、未来的
    后台任务），跨用户读就会静默发生。这里正面钉住仓储自己的边界。
    """
    with memory_env.Session() as s:
        s.add(AgentSession(
            id="s-u2", user_id="u2", entry_source="x",
            context_refs=[], missing_context=[], warnings=[],
        ))
        s.commit()

    assert AgentSessionStore(memory_env.db, "u1").get("s-u2") is None
    assert AgentSessionStore(memory_env.db, "u2").get("s-u2") is not None


# ===========================================================================
# C. 长期记忆
# ===========================================================================
@pytest.mark.parametrize("bad", [
    "x" * 501,                                  # 超长：正文搬运的典型特征
    "第一行\n第二行\n第三行\n第四行",            # 多行
    "my key is sk-abcdef123456",                # 凭据特征
    "Authorization: Bearer abc",                # 凭据特征
    "   ",                                      # 空
])
def test_memory_content_is_guarded_structurally(bad):
    """15.3 节的禁止清单用**结构约束**拦住，不靠事后审计。

    判据都可用一句话复核：长度、行数、凭据特征串。不做"这段是不是简历正文"
    的语义判断——那种判断不可能可靠。
    """
    with pytest.raises(MemoryContentRejected):
        MemoryRepository.__init__  # noqa: B018  （避免误用实例方法）
        from app.modules.career_agent.memory_repository import (
            assert_memory_content_allowed,
        )
        assert_memory_content_allowed(bad)


def test_proposed_memory_never_enters_the_vector_index(memory_env):
    """``proposed`` 记忆**不进索引**——这是结构性保证，不是"召回了再过滤"。

    断言分两层：
    1. 语义层：``recall`` 召不到它；
    2. **结构层**：向量库里根本没有这个 id（``collection.get(ids=...)`` 为空）。
    第 2 层才是关键——只断言第 1 层的话，"过滤逻辑恰好对了"与"根本没进索引"
    分不出来，而后者才是我们想要的不变量。
    """
    repo = MemoryRepository(memory_env.db, "u1")
    proposed = repo.create(
        content="希望只看北京的机会", memory_type="preference",
        source="system_event", status="proposed",
    )
    assert repo.recall("北京 机会") == []

    got = get_memory_collection().get(ids=[proposed.memory_id])
    assert got["ids"] == [], "proposed 记忆不得进入向量索引"

    # 确认之后才进索引、才召回得到
    repo.confirm(proposed.memory_id)
    hits = repo.recall("北京 机会", k=5)
    assert [h["memory_id"] for h in hits] == [proposed.memory_id]
    assert get_memory_collection().get(ids=[proposed.memory_id])["ids"] == [proposed.memory_id]


def test_confirm_is_idempotent_and_only_user_path(memory_env):
    """重复确认不改变事实；且 confirmation 会把来源标为 ``user_confirmed``。"""
    repo = MemoryRepository(memory_env.db, "u1")
    row = repo.create(
        content="目标岗位偏后端", memory_type="decision",
        source="system_event", status="proposed", source_refs=["jd.get:c1:result"],
    )
    first = repo.confirm(row.memory_id)
    second = repo.confirm(row.memory_id)
    assert first.status == second.status == "confirmed"
    assert second.source == "user_confirmed"


def test_delete_removes_both_record_and_vector(memory_env):
    """删除必须**同时**删结构化记录与向量索引（8.2 / 15.2 节）。"""
    repo = MemoryRepository(memory_env.db, "u1")
    row = repo.create(
        content="只看北京", memory_type="preference",
        source="user_explicit", status="confirmed",
    )
    assert repo.recall("北京") != []

    assert repo.delete(row.memory_id) is True
    assert repo.get(row.memory_id) is None
    assert repo.recall("北京") == []
    assert get_memory_collection().get(ids=[row.memory_id])["ids"] == []
    # 删两次：第二次返回 False，不抛
    assert repo.delete(row.memory_id) is False


def test_conflict_key_supersedes_old_memory(memory_env):
    """记忆冲突时**保留新记录、旧的标 superseded**，不静默覆盖（15.2 节）。"""
    repo = MemoryRepository(memory_env.db, "u1")
    old = repo.create(
        content="目标城市：上海", memory_type="preference",
        source="user_explicit", status="confirmed", conflict_key="target_city",
    )
    new = repo.create(
        content="目标城市：北京", memory_type="preference",
        source="user_explicit", status="confirmed", conflict_key="target_city",
    )

    old_row = repo.get(old.memory_id)
    assert old_row is not None, "旧记忆必须留痕，不能删掉"
    assert old_row.status == "superseded"
    assert old_row.superseded_by == new.memory_id
    # 旧的已撤出召回面（向量也删了）
    assert get_memory_collection().get(ids=[old.memory_id])["ids"] == []
    hits = [h["memory_id"] for h in repo.recall("目标城市", k=10)]
    assert hits == [new.memory_id]


def test_memories_are_tenant_isolated(memory_env):
    """跨用户：A 写的记忆，B 既取不到也召不到。"""
    a = MemoryRepository(memory_env.db, "u1")
    b = MemoryRepository(memory_env.db, "u2")
    row = a.create(
        content="A 的私密偏好：只投大厂", memory_type="preference",
        source="user_explicit", status="confirmed",
    )

    assert b.get(row.memory_id) is None
    assert b.recall("大厂") == []
    assert b.list() == []
    assert a.recall("大厂") != []


# ===========================================================================
# D. 写操作策略（纯函数，不依赖 DB）
# ===========================================================================
def test_action_whitelist_is_closed():
    """``type`` → 工具的映射必须是**闭集合**。

    它是"模型产出的 type 能不能走写路径"的唯一开关。若这里做了模糊匹配
    （例如按前缀猜），模型输出一个近似名字就能碰到写工具。
    """
    assert action_tool_for("update_application_status") == "application.update_status"
    for bad in ("", "optimize_resume", "application.update_status", "UPDATE_APPLICATION_STATUS"):
        assert action_tool_for(bad) is None


def test_idempotency_key_is_deterministic():
    """幂等键必须确定：否则 interrupt 恢复时的重放会造出第二条意图。"""
    args = {"application_id": "app1", "new_status": "applied"}
    a = new_idempotency_key("s1", "r1", "application.update_status", args)
    b = new_idempotency_key("s1", "r1", "application.update_status", args)
    assert a == b
    # 参数顺序不同、内容相同 ⇒ 同一个键（json sort_keys）
    c = new_idempotency_key("s1", "r1", "application.update_status",
                            {"new_status": "applied", "application_id": "app1"})
    assert c == a
    # 换任一维度都要变
    assert new_idempotency_key("s2", "r1", "application.update_status", args) != a
    assert new_idempotency_key("s1", "r2", "application.update_status", args) != a


def test_replaying_prepare_does_not_create_a_second_intent(beta_env):
    """重放「提议」这一步，只能有一条意图行。

    这条测的是**幂等键确定性到底守护了什么**：把 ``prepare_write_action``
    重放一遍（例如进程在"落库完成、断点尚未写入"之间挂掉后重跑），
    必须命中同一行、返回同一个 ``action_id``——否则用户会看到两条待确认，
    确认其中一条、另一条永远悬在那里。

    ⚠️ 与 ``test_duplicate_confirm_is_idempotent`` 的区别：那条守的是
    **重复确认**（判据是意图状态已 ``succeeded``，与幂等键无关）；这条守的是
    **重复提议**。两条看起来相似，但把幂等键改成随机值时只有这条会变红
    —— 这一点是用变异测试确认过的，不是推测。
    """
    from app.modules.career_agent.policies import (
        ActionIntentStore,
        new_action_id,
    )
    from app.modules.career_agent.state import ActionIntent

    arguments = {"application_id": "app1", "expected_status": "saved",
                 "new_status": "applied", "reason": "用户说已投递"}
    key = new_idempotency_key("s1", "r1", "application.update_status", arguments)

    with beta_env.Session() as db:
        store = ActionIntentStore(db, "u1")
        first = store.upsert_pending(
            ActionIntent(action_id=new_action_id(key), type="application.update_status",
                         arguments=arguments, reason="用户说已投递",
                         status="pending_confirmation", idempotency_key=key),
            session_id="s1", run_id="r1",
        )
        second = store.upsert_pending(
            ActionIntent(action_id=new_action_id(key), type="application.update_status",
                         arguments=arguments, reason="用户说已投递",
                         status="pending_confirmation", idempotency_key=key),
            session_id="s1", run_id="r1",
        )
        rows = db.query(AgentActionIntent).filter(
            AgentActionIntent.idempotency_key == key,
        ).all()

    assert len(rows) == 1, f"重放造出了 {len(rows)} 条意图"
    assert second.action_id == first.action_id


def _intent_row(**overrides) -> AgentActionIntent:
    base = dict(
        action_id="act1", user_id="u1", session_id="s1", run_id="r1",
        type="application.update_status",
        arguments={"application_id": "app1", "expected_status": "saved", "new_status": "applied"},
        status="pending_confirmation", idempotency_key="k1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    base.update(overrides)
    return AgentActionIntent(**base)


def test_resume_check_rejects_foreign_user():
    """恢复前必须重新校验用户（21.5 节）。"""
    check = check_resume_allowed(_intent_row(user_id="u2"), user_id="u1")
    assert check.ok is False
    assert check.code == "action_not_found"


def test_resume_check_rejects_expired():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    check = check_resume_allowed(_intent_row(expires_at=past), user_id="u1")
    assert check.ok is False and check.code == "action_expired"


def test_resume_check_rejects_settled_and_non_write_types():
    check = check_resume_allowed(_intent_row(status="succeeded"), user_id="u1")
    assert check.ok is False and check.code == "action_already_succeeded"

    check = check_resume_allowed(_intent_row(type="jd.get"), user_id="u1")
    assert check.ok is False and check.code == "action_not_allowed"


def test_resume_check_passes_for_valid_intent():
    assert check_resume_allowed(_intent_row(), user_id="u1").ok is True
    assert check_resume_allowed(None, user_id="u1").ok is False


# ===========================================================================
# E. 确认写入（端到端）
# ===========================================================================
def _application_status(env, app_id="app1") -> str:
    with env.Session() as s:
        return s.query(Application).filter(Application.id == app_id).first().status


def _event_count(env, app_id="app1") -> int:
    with env.Session() as s:
        return s.query(ApplicationEvent).filter(
            ApplicationEvent.application_id == app_id,
        ).count()


def test_write_action_pauses_for_confirmation_without_writing(beta_env, monkeypatch):
    """**beta 最重要的一条**：提议之后必须停在等确认，且业务数据一行都不能动。

    判据是三重的，缺一不可：
    1. ``status == waiting_confirmation`` 且有 ``pending_action``；
    2. ``applications.status`` 仍是 ``saved``；
    3. ``application_events`` 仍是 0 条。

    只断言第 1 条会漏掉"状态对了但已经偷写了"这种最坏情况。
    """
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    data = _run(beta_env, session_id, "我已经投递了，帮我更新状态").json()["data"]

    assert data["status"] == "waiting_confirmation"
    assert data["pending_action"] is not None
    assert data["pending_action"]["tool"] == "application.update_status"
    assert data["pending_action"]["expected_status"] == "saved"
    assert data["pending_action"]["new_status"] == "applied"
    assert data["wrote_business_data"] is False
    # 还没确认 ⇒ 这不是"写失败"，不能提前报成失败（否则用户会以为操作已经出错了）
    assert data["write_failure"] is None

    assert _application_status(beta_env) == "saved", "确认前不得改业务状态"
    assert _event_count(beta_env) == 0, "确认前不得写业务事件"


def test_confirm_executes_the_write(beta_env, monkeypatch):
    """用户确认后才真正执行，并留下业务事件（投递时间线的产品价值）。"""
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]
    action_id = first["pending_action"]["action_id"]

    resp = beta_env.client.post(
        f"{BASE}/sessions/{session_id}/actions/{action_id}", json={"approved": True},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["wrote_business_data"] is True
    assert data["pending_action"] is None
    assert data["write_failure"] is None, "写成功了不得同时报失败原因"

    assert _application_status(beta_env) == "applied"
    assert _event_count(beta_env) == 1
    with beta_env.Session() as s:
        event = s.query(ApplicationEvent).filter(
            ApplicationEvent.application_id == "app1",
        ).first()
        assert event.from_status == "saved" and event.to_status == "applied"


def test_reject_leaves_business_data_untouched(beta_env, monkeypatch):
    """拒绝 = 什么都不发生，但意图要落 ``cancelled``（审计留痕）。"""
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]

    resp = beta_env.client.post(
        f"{BASE}/sessions/{session_id}/actions/{first['pending_action']['action_id']}",
        json={"approved": False},
    )
    assert resp.status_code == 200
    rejected = resp.json()["data"]
    assert rejected["status"] == "cancelled"
    # 关键反向对照：**用户主动拒绝不是"写失败"**。若把裁决结果丢掉、只看
    # "意图没落 succeeded"，这里就会冒出一个 write_failure，页面会误导用户。
    assert rejected["write_failure"] is None
    assert _application_status(beta_env) == "saved"
    assert _event_count(beta_env) == 0
    with beta_env.Session() as s:
        assert s.query(AgentActionIntent).filter(
            AgentActionIntent.status == "cancelled",
        ).count() == 1


def test_cas_conflict_blocks_the_write(beta_env, monkeypatch):
    """提议之后业务状态被别人改过 ⇒ CAS 失败，**不覆盖**别人的改动。

    这是 ``expected_status`` 存在的全部意义：Agent 基于"它看到的旧状态"提议，
    而用户/其它端可能已经改过。没有 CAS 的话，确认动作会把那条改动**抹掉**。
    """
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]

    # 模拟"另一边"把状态改成了 online_test
    with beta_env.Session() as s:
        s.query(Application).filter(Application.id == "app1").first().status = "online_test"
        s.commit()

    resp = beta_env.client.post(
        f"{BASE}/sessions/{session_id}/actions/{first['pending_action']['action_id']}",
        json={"approved": True},
    )
    data = resp.json()["data"]
    assert data["status"] == "failed", "CAS 失败必须表现为失败，不能装成成功"
    # 前端要区分「版本冲突」和「写库失败」就必须拿到**机器可判的取值**。
    # 只给中文文案的话，页面只能去匹配句子——文案一改就静默失效。
    failure = data["write_failure"]
    assert failure is not None, "写失败必须带结构化原因"
    assert failure["code"] == "status_conflict"
    assert failure["tool"] == "application.update_status"
    assert failure["message"], "结构化原因必须带可展示的文案"
    assert _application_status(beta_env) == "online_test", "不得覆盖别人的改动"
    assert _event_count(beta_env) == 0
    with beta_env.Session() as s:
        row = s.query(AgentActionIntent).filter(
            AgentActionIntent.action_id == first["pending_action"]["action_id"],
        ).first()
        assert row.status == "failed"
        assert row.error_code == "status_conflict"


def test_duplicate_confirm_is_idempotent(beta_env, monkeypatch):
    """重复确认只写一次业务数据。

    这条同时是幂等键的**行为证据**：第二次确认会被 ``check_resume_allowed``
    识别为"已结束"，走幂等分支返回，而不是再执行一次写工具。
    """
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]
    action_id = first["pending_action"]["action_id"]

    url = f"{BASE}/sessions/{session_id}/actions/{action_id}"
    first_resp = beta_env.client.post(url, json={"approved": True})
    second_resp = beta_env.client.post(url, json={"approved": True})

    assert first_resp.status_code == 200 and second_resp.status_code == 200
    assert _application_status(beta_env) == "applied"
    assert _event_count(beta_env) == 1, "重复确认不得写出第二条事件"


def test_expired_action_is_rejected(beta_env, monkeypatch):
    """过期确认返回 410，且**不执行**写操作。"""
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]
    action_id = first["pending_action"]["action_id"]

    with beta_env.Session() as s:
        row = s.query(AgentActionIntent).filter(
            AgentActionIntent.action_id == action_id,
        ).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()

    resp = beta_env.client.post(
        f"{BASE}/sessions/{session_id}/actions/{action_id}", json={"approved": True},
    )
    assert resp.status_code == 410
    assert _application_status(beta_env) == "saved"
    assert _event_count(beta_env) == 0


def test_foreign_action_is_not_found(beta_env, monkeypatch):
    """跨用户确认一律 404（不区分"不存在"与"无权限"）。"""
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]
    action_id = first["pending_action"]["action_id"]

    beta_env.holder["user"] = User(id="u2", name="乙")
    resp = beta_env.client.post(
        f"{BASE}/sessions/{session_id}/actions/{action_id}", json={"approved": True},
    )
    assert resp.status_code == 404
    # 别人的会话也读不到
    assert beta_env.client.get(f"{BASE}/sessions/{session_id}").status_code == 404
    assert _application_status(beta_env) == "saved"


def test_no_application_context_means_no_write_action(beta_env, monkeypatch):
    """上下文中没有投递记录 ⇒ 即使模型提议了写行动，也**不会**进入确认流程。

    这是 ``_write_plan`` 的第一条必要条件：``application_id`` 与
    ``expected_status`` 都只能来自上下文，没有对象就没有合法期望值。
    """
    _use_llm(monkeypatch, WriteIntentLLM)
    session_id = _create(beta_env).json()["data"]["session_id"]  # 不带 application_id
    data = _run(beta_env, session_id, "我已经投递了").json()["data"]

    assert data["status"] == "completed"
    assert data["pending_action"] is None
    assert data["wrote_business_data"] is False
    assert _application_status(beta_env) == "saved"


def test_write_tool_rejects_status_outside_allowlist(beta_env, monkeypatch):
    """目标状态不在 Agent 可写集合里 ⇒ 写工具返回 failed，业务数据不变。"""
    _use_llm(monkeypatch, lambda: WriteIntentLLM(new_status="accepted"))
    session_id = _create(beta_env, application_id="app1").json()["data"]["session_id"]
    first = _run(beta_env, session_id, "我已经投递了").json()["data"]
    action_id = first["pending_action"]["action_id"]

    resp = beta_env.client.post(
        f"{BASE}/sessions/{session_id}/actions/{action_id}", json={"approved": True},
    )
    assert resp.json()["data"]["status"] == "failed"
    assert _application_status(beta_env) == "saved"
    assert _event_count(beta_env) == 0


# ===========================================================================
# F. 记忆与 API
# ===========================================================================
def test_decision_memory_is_proposed_after_a_run(beta_env, monkeypatch):
    """跑完一轮会落一条 ``proposed`` 决策记忆——**但不会自动 confirmed**。"""
    _use_llm(monkeypatch, ReadOnlyLLM)
    session_id = _create(beta_env).json()["data"]["session_id"]
    _run(beta_env, session_id)

    rows = beta_env.client.get(f"{BASE}/memories").json()["data"]
    proposed = [r for r in rows if r["status"] == "proposed"]
    assert len(proposed) == 1
    assert proposed[0]["memory_type"] == "decision"
    assert proposed[0]["source_refs"], "记忆必须带来源引用"


def test_user_can_confirm_and_delete_memory_via_api(beta_env, monkeypatch):
    """用户确认后进 confirmed / 进索引；删除后彻底消失。"""
    _use_llm(monkeypatch, ReadOnlyLLM)
    session_id = _create(beta_env).json()["data"]["session_id"]
    _run(beta_env, session_id)
    memory_id = beta_env.client.get(f"{BASE}/memories").json()["data"][0]["memory_id"]

    confirmed = beta_env.client.post(f"{BASE}/memories/{memory_id}/confirm").json()["data"]
    assert confirmed["status"] == "confirmed"

    repo = MemoryRepository(beta_env.Session(), "u1")
    assert repo.recall("Redis 证据") != []

    deleted = beta_env.client.delete(f"{BASE}/memories/{memory_id}")
    assert deleted.status_code == 200
    assert beta_env.client.get(f"{BASE}/memories").json()["data"] == []
    assert get_memory_collection().get(ids=[memory_id])["ids"] == []


def test_explicit_preference_is_confirmed_immediately(beta_env):
    """用户亲口说的偏好直接 confirmed / user_explicit（8.2 节）。"""
    data = beta_env.client.post(
        f"{BASE}/memories",
        json={"content": "只看北京的机会", "conflict_key": "target_city"},
    ).json()["data"]
    assert data["status"] == "confirmed"
    assert data["source"] == "user_explicit"

    # 凭据/超长内容被拒（API 层也要拦，不能只靠仓储）
    bad = beta_env.client.post(
        f"{BASE}/memories", json={"content": "token: sk-abcdef123456"},
    )
    assert bad.status_code == 400


def test_memory_api_is_tenant_scoped(beta_env):
    """他人的记忆：读不到、确认不了、删不掉——统一 404。"""
    with beta_env.Session() as s:
        s.add(AgentMemory(
            memory_id="m-u2", user_id="u2", memory_type="preference",
            status="confirmed", content="乙的偏好", normalized_content="乙的偏好",
            source="user_explicit", source_refs=[], confidence=1.0, importance=1.0,
        ))
        s.commit()

    assert beta_env.client.get(f"{BASE}/memories").json()["data"] == []
    assert beta_env.client.post(f"{BASE}/memories/m-u2/confirm").status_code == 404
    assert beta_env.client.delete(f"{BASE}/memories/m-u2").status_code == 404


def test_recalled_memory_reaches_the_model_payload(beta_env, monkeypatch):
    """已确认的记忆必须真的进 compose 轮的 payload（否则"记忆"只是个摆设）。"""
    from app.modules.career_agent import routes as routes_mod

    captured = {}

    class MemoryAwareLLM(ReadOnlyLLM):
        def invoke(self, messages):
            if not self._selecting:
                content = getattr(messages[-1], "content", "") or ""
                try:
                    captured["payload"] = json.loads(
                        content[content.find("{"): content.rfind("}") + 1]
                    )
                except Exception:  # noqa: BLE001
                    captured["payload"] = {}
            return super().invoke(messages)

    # 先写入一条已确认偏好（召回要靠它）
    with beta_env.Session() as s:
        s.add(AgentMemory(
            memory_id="m-pref", user_id="u1", memory_type="preference",
            status="confirmed", content="只看北京的后端机会",
            normalized_content="只看北京的后端机会", source="user_explicit",
            source_refs=["user:explicit"], confidence=1.0, importance=1.0,
        ))
        s.commit()
    MemoryRepository(beta_env.Session(), "u1")  # 触发集合创建
    repo = MemoryRepository(beta_env.Session(), "u1")
    # 直接确认写入向量（API 路径已在别的用例覆盖）
    from app.modules.career_agent.memory_repository import get_memory_collection, _embed
    from app.modules.career_agent.memory_repository import normalize_content
    get_memory_collection().upsert(
        ids=["m-pref"], embeddings=[_embed(normalize_content("只看北京的后端机会"))],
        documents=["只看北京的后端机会"],
        metadatas=[{"user_id": "u1", "memory_type": "preference", "status": "confirmed"}],
    )

    _use_llm(monkeypatch, MemoryAwareLLM)
    session_id = _create(beta_env).json()["data"]["session_id"]
    _run(beta_env, session_id, "北京的后端岗位适合我吗")

    payload = captured.get("payload") or {}
    recalled = payload.get("recalled_memories") or []
    assert any(m["memory_id"] == "m-pref" for m in recalled), (
        f"已确认记忆必须进 compose payload，实际 recalled={recalled}"
    )
    assert routes_mod is not None
