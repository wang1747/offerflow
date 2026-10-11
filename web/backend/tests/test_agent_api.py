"""career_agent alpha API（设计文档阶段 4）验收测试。

对应设计文档 22.1 阶段 4：**session、thread、context hash 和只读返回**；
不通过条件是「跨用户访问或写入业务数据」。下面每个用例都对应其中一条。

测试用的假模型只做两件事：先按线名选工具，再把真实 ``available_evidence_refs``
原样回抄。这样测的是我们的管道，而不是某个模型的随机表现。
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.models.application import Application
from app.models.jd import JobDescription
from app.models.match import MatchResult
from app.models.resume import Resume
from app.models.user import User
from app.modules.auth.routes import get_current_user_required
from app.modules.career_agent import routes as agent_routes

BASE = "/api/v1/agent"


# ===========================================================================
# 夹具与假模型
# ===========================================================================
@pytest.fixture
def agent_env(tmp_path):
    """带 JD/简历的真实 FastAPI TestClient；身份可在 u1/u2 间切换。"""
    # 记忆的向量库必须指向本用例自己的临时目录，否则会写进仓库根的 chroma_db
    from app.config import get_settings
    get_settings().chroma_persist_path = str(tmp_path / "chroma")

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'agent.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as s:
        s.add(User(id="u1", name="甲"))
        s.add(User(id="u2", name="乙"))
        s.add(JobDescription(
            id="jd1", user_id="u1", raw_text="Python 后端开发，需要 Redis 与高并发经验。",
            company="A公司", position="后端工程师", must_have=["Python", "Redis"],
        ))
        s.add(Resume(id="r1", user_id="u1", version=1, raw_text="张三 后端工程师 " * 40))
        # u2 也有自己的一套，用来做"两个用户并发跑"的隔离验证
        s.add(JobDescription(
            id="jd2", user_id="u2", raw_text="机密岗位：Rust 基础建设。",
            company="机密公司", position="Rust 工程师", must_have=["Rust"],
        ))
        s.add(Resume(id="r2", user_id="u2", version=1, raw_text="李四 Rust 工程师 " * 40))
        s.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    holder = {"user": User(id="u1", name="甲")}

    def override_current_user(request: Request):
        # 显式带头则按头切身份（并发用例需要同一时刻两个不同用户）
        uid = request.headers.get("x-test-user")
        return User(id=uid, name=uid) if uid else holder["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_required] = override_current_user
    # 进程内会话/检查点必须逐用例清空，否则用例之间互相污染
    agent_routes._reset_agent_api_state()
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, holder=holder, Session=Session)
    app.dependency_overrides.clear()
    agent_routes._reset_agent_api_state()
    engine.dispose()


def _refs_from(messages):
    """只从 **Human 轮**取 available_evidence_refs。

    ⚠️ 不能遍历所有消息：``COMPOSE_PROMPT`` 本身就写着 ``available_evidence_refs``
    这个词（它是给模型的字段说明），按字符串找会先命中 System 提示词，
    然后拿提示词里的 JSON 片段去解析 → JSONDecodeError。
    """
    for m in messages:
        if not isinstance(m, HumanMessage):
            continue
        content = getattr(m, "content", "") or ""
        if "available_evidence_refs" in content:
            raw = content[content.find("{"): content.rfind("}") + 1]
            return json.loads(raw).get("available_evidence_refs") or []
    return []


class GoodLLM:
    """正常的假模型：选两个只读工具，再给出合法结论。

    ``jd_id`` / ``resume_id`` 可参数化——做并发隔离验证时，
    两个用户各自的模型必须去读**自己的**对象。
    """

    def __init__(self, jd_id="jd1", resume_id="r1"):
        self._jd_id = jd_id
        self._resume_id = resume_id
        self._selecting = False

    def bind_tools(self, tools, **kwargs):
        self._selecting = True
        return self

    def invoke(self, messages):
        if self._selecting:
            self._selecting = False
            return AIMessage(content="", tool_calls=[
                {"name": "jd_get", "args": {"jd_id": self._jd_id},
                 "id": "c1", "type": "tool_call"},
                {"name": "resume_get", "args": {"resume_id": self._resume_id},
                 "id": "c2", "type": "tool_call"},
            ])
        refs = _refs_from(messages)
        return AIMessage(content=json.dumps({
            "decision": "optimize_first",
            "confidence": 0.8,
            "summary": "岗位值得投，但建议先补 Redis 证据。",
            "actions": [{
                "type": "optimize_resume", "title": "补充 Redis 项目证据",
                "priority": 1, "reason": "JD 把 Redis 列为必备。",
                "evidence_refs": refs[:1], "requires_confirmation": True,
            }],
            "evidence": [{"ref": r, "source": "jd.get"} for r in refs],
            "missing_info": [],
        }, ensure_ascii=False))


class BadLLM:
    """永远给不出合法 JSON 的假模型：用来制造一次**真失败**的运行。"""

    def invoke(self, messages):
        return AIMessage(content="这不是 JSON")


class RecordingLLM(GoodLLM):
    """记录**真正发出去**的消息，便于对请求本身做结构断言。

    ⚠️ 缺陷的形态是"发出去的消息不合协议"，而假模型不校验协议、照常正常回答——
    所以只断言"这一轮跑成了"永远抓不住它。必须把请求录下来单独看。
    """

    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def bind_tools(self, tools, **kwargs):
        super().bind_tools(tools, **kwargs)
        return self

    def invoke(self, messages):
        self._sink.append(("select" if self._selecting else "compose", list(messages)))
        return super().invoke(messages)


def _use_llm(monkeypatch, llm_factory):
    """注入模型工厂：**每次请求新建一个实例**。

    假模型带可变状态（``_selecting``），多请求共用一个实例会互相干扰——
    真实场景里每次请求本来也会新建 LLM 客户端。
    """
    monkeypatch.setattr(
        agent_routes, "get_user_llm_or_raise", lambda user: llm_factory(),
    )


def _create(env, **overrides):
    body = {"entry_source": "jd_page", "jd_id": "jd1", "resume_id": "r1"}
    body.update(overrides)
    return env.client.post(f"{BASE}/sessions", json=body)


def _run(env, session_id, message="我下一步该做什么？"):
    return env.client.post(
        f"{BASE}/sessions/{session_id}/messages", json={"message": message},
    )


# ===========================================================================
# 会话创建：session_id == thread_id、上下文指纹、同上下文复用
# ===========================================================================
def test_create_session_exposes_thread_id_and_context_hash(agent_env):
    r = _create(agent_env)
    assert r.status_code == 200
    data = r.json()["data"]

    # 21.6：session_id 即 LangGraph thread_id
    assert data["thread_id"] == data["session_id"]
    assert data["session_id"]
    # 阶段 4 验收明确要求返回 context hash
    assert data["context_hash"]
    assert data["needs_input"] is False
    assert data["resumed"] is False
    # beta：长期记忆已启用；Checkpointer 类型必须如实暴露
    # （``memory`` 是危险的静默降级，只会在"重启后会话消失"时暴露）
    assert data["memory_enabled"] is True
    assert data["checkpointer"] in ("postgres", "memory", "uninitialized")
    assert data["has_pending_action"] is False


def test_create_session_same_context_resumes(agent_env):
    """相同用户 + 相同上下文 ⇒ 复用同一会话（2.6 节）。"""
    first = _create(agent_env).json()["data"]
    second = _create(agent_env).json()["data"]

    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True
    assert second["context_hash"] == first["context_hash"]


def test_create_session_without_context_returns_needs_input(agent_env):
    """不传 JD/简历就不猜——缺失项交给前端选择器补齐（2.10 节）。

    注意：只有**一份**简历时 ContextBuilder 会自动选中（2.4 节允许），
    因此这个场景缺的只有 JD。
    """
    r = _create(agent_env, jd_id=None, resume_id=None)
    data = r.json()["data"]

    assert data["needs_input"] is True
    assert {m["type"] for m in data["missing_context"]} == {"jd"}


def test_ambiguous_resumes_are_never_auto_guessed(agent_env):
    """多份简历时必须标记歧义、等用户选——不允许自动猜一份（2.4 节）。"""
    with agent_env.Session() as s:
        s.add(Resume(id="r1b", user_id="u1", version=1, raw_text="李四 前端工程师 " * 40))
        s.commit()

    data = _create(agent_env, jd_id="jd1", resume_id=None).json()["data"]
    reasons = {m["type"]: m["reason"] for m in data["missing_context"]}

    assert data["needs_input"] is True
    assert reasons == {"resume": "ambiguous"}


def test_create_session_accepts_application_id_as_context(agent_env):
    """beta 起投递记录是**一等上下文**。

    alpha 只支持 JD/简历，application_id 会被明确告知忽略；beta 的写工具需要
    ``application`` 对象作为 CAS 期望值的来源（``expected_status`` 只能从上下文取），
    所以它必须真的进 ``context_refs``，而不是"接受但丢弃"。
    """
    with agent_env.Session() as s:
        s.add(Application(
            id="app-1", user_id="u1", company="甲公司",
            position="后端工程师", status="saved",
        ))
        s.commit()

    data = _create(agent_env, application_id="app-1").json()["data"]
    assert {r["type"] for r in data["context_refs"]} == {"jd", "resume", "application"}
    assert data["needs_input"] is False


# ===========================================================================
# 跨用户隔离：不存在与无权限返回同一个 404
# ===========================================================================
def test_session_is_private_to_its_owner(agent_env):
    session_id = _create(agent_env).json()["data"]["session_id"]

    agent_env.holder["user"] = User(id="u2", name="乙")
    assert agent_env.client.get(f"{BASE}/sessions/{session_id}").status_code == 404
    assert _run(agent_env, session_id).status_code == 404
    assert agent_env.client.get(f"{BASE}/sessions/{session_id}/trace").status_code == 404


def test_unknown_and_foreign_session_are_indistinguishable(agent_env):
    """404 文案必须一致，否则可以用差异探测出"存在但无权限"。"""
    session_id = _create(agent_env).json()["data"]["session_id"]

    agent_env.holder["user"] = User(id="u2", name="乙")
    foreign = agent_env.client.get(f"{BASE}/sessions/{session_id}")
    missing = agent_env.client.get(f"{BASE}/sessions/does-not-exist")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["detail"] == missing.json()["detail"]


def test_agent_api_requires_auth(auth_client):
    assert auth_client.get(f"{BASE}/tools").status_code == 401
    assert auth_client.post(f"{BASE}/sessions", json={}).status_code == 401


# ===========================================================================
# 运行消息：每条消息独立 run_id、只读、上下文缺失不调模型
# ===========================================================================
def test_each_message_gets_its_own_run_id(agent_env, monkeypatch):
    """21.6：一个 session 可包含多个 run，每条用户消息一个独立 run_id。

    ⚠️ 必须注入假模型：这条用例会**真的跑两条消息**。不注入的话它会走
    ``get_user_llm_or_raise`` 的服务器默认 Key 分支——在有平台 Key 的容器里
    等于真调两次模型（烧配额），在没有 Key 的本地则直接 409 报 KeyError，
    测试结果因此取决于环境而不是代码。
    """
    _use_llm(monkeypatch, GoodLLM)
    session_id = _create(agent_env).json()["data"]["session_id"]

    first = _run(agent_env, session_id).json()["data"]
    second = _run(agent_env, session_id).json()["data"]

    assert first["run_id"] and second["run_id"]
    assert first["run_id"] != second["run_id"]
    # 两条消息都记进 trace
    trace = agent_env.client.get(f"{BASE}/sessions/{session_id}/trace").json()["data"]
    assert [r["run_id"] for r in trace["runs"]] == [first["run_id"], second["run_id"]]


def test_missing_context_returns_needs_input_without_calling_model(agent_env, monkeypatch):
    """上下文不唯一时**确定性**返回 needs_input：不猜、不调模型、不烧配额。

    这里用「不属于自己的 jd/resume」来制造缺失：与 capabilities 层策略一致，
    not_found 被统一当成缺失上下文，不区分"不存在"与"无权限"。
    """
    created = _create(
        agent_env, jd_id="jd-not-mine", resume_id="r-not-mine",
    ).json()["data"]
    session_id = created["session_id"]
    assert {m["type"] for m in created["missing_context"]} == {"jd", "resume"}

    def _must_not_be_called(user):       # pragma: no cover - 被调用即失败
        raise AssertionError("上下文缺失时不应调用模型")

    monkeypatch.setattr(agent_routes, "get_user_llm_or_raise", _must_not_be_called)

    data = _run(agent_env, session_id).json()["data"]
    assert data["status"] == "needs_input"
    assert data["needs_input"] is True
    assert data["tool_calls"] == []
    assert {m["type"] for m in data["missing_context"]} == {"jd", "resume"}
    # 缺失原因统一是 not_found，不区分"不存在"与"无权限"
    assert {m["reason"] for m in data["missing_context"]} == {"not_found"}


def test_run_is_read_only_and_returns_traceable_evidence(agent_env, monkeypatch):
    """端到端：session → run → 只读结论，且**一行业务数据都没写**。"""
    _use_llm(monkeypatch, GoodLLM)
    session_id = _create(agent_env).json()["data"]["session_id"]

    with agent_env.Session() as probe:
        before = (
            probe.query(Resume).count(),
            probe.query(JobDescription).count(),
            probe.query(MatchResult).count(),
        )

    data = _run(agent_env, session_id).json()["data"]

    with agent_env.Session() as probe:
        after = (
            probe.query(Resume).count(),
            probe.query(JobDescription).count(),
            probe.query(MatchResult).count(),
        )
    assert before == after, "alpha 只读切片不得写任何业务数据"

    assert data["status"] == "completed"
    assert data["errors"] == []
    assert data["output"]["decision"] == "optimize_first"
    assert data["context_hash"]
    # beta：这一轮是**只读规划**（用户没确认任何写操作）⇒ 业务写入为 false。
    # 判据是状态机（approval.decision + intent.status）而不是"有没有调用过写工具"。
    assert data["wrote_business_data"] is False
    assert data["pending_action"] is None

    # 只调了只读工具，且都成功
    called = {t["tool"]: t["status"] for t in data["tool_calls"]}
    assert called == {"jd.get": "succeeded", "resume.get": "succeeded"}

    # 证据可追溯：ref 必须由真实工具调用推导（{tool}:{call_id}:result）
    refs = [e["ref"] for e in data["output"]["evidence"]]
    assert refs
    for ref in refs:
        tool, call_id, suffix = ref.split(":")
        assert tool in ("resume.get", "jd.get", "match.get")
        assert len(call_id) == 36          # uuid4
        assert suffix == "result"

    # action 的引用必须落在 evidence 集合里
    action_refs = [r for a in data["output"]["actions"] for r in a["evidence_refs"]]
    assert set(action_refs) <= set(refs)


def test_second_run_reports_only_its_own_errors(agent_env, monkeypatch):
    """``errors`` 的 reducer 是 operator.add（保留历史），但 API 只返回本次运行的错误。

    先跑一次**真失败**的运行（模型给不出合法 JSON），再跑一次成功的：
    第二次必须 ``errors == []``，否则前端会把上一次的失败当成这次的失败。
    """
    _use_llm(monkeypatch, BadLLM)
    session_id = _create(agent_env).json()["data"]["session_id"]

    failed = _run(agent_env, session_id).json()["data"]
    assert failed["status"] == "failed"
    assert failed["errors"]

    _use_llm(monkeypatch, GoodLLM)
    ok = _run(agent_env, session_id).json()["data"]
    assert ok["status"] == "completed"
    assert ok["errors"] == []


def test_multi_turn_second_message_is_a_full_run(agent_env, monkeypatch, assert_protocol_clean):
    """同一会话的**第二条消息**：发出去的每个请求都必须协议干净，且这一轮要是完整的一轮。

    2026-10-04 生产冒烟抓到的缺陷（严重度：线上第二轮直接降级）：
    ``select_read_tools`` 把 ``state["messages"]`` 原样发给模型，而
    ``execute_read_tools`` 的结果**从不写回 messages** ⇒ 历史里留下"带 ``tool_calls``
    却没有配对 tool 消息"的孤儿 AIMessage，**首轮必过、第二轮起必 400**（真实 provider
    直接 BadRequestError）。异常被 ``except`` 降级成一行 WARNING，失败路径照样返回
    ``completed`` ⇒ 从接口上看是"成功"，实际那一轮零工具、零证据。

    这条用例同时钉住三件事：
    1. 请求结构（不能携带孤儿 tool_calls / 中间插 System）；
    2. 第二轮仍然是**完整**的一轮（有工具、有证据、无错误）；
    3. ``RunView`` 只描述**本次运行**（工具调用不跨轮累积）。
    """
    sink = []
    _use_llm(monkeypatch, lambda: RecordingLLM(sink))
    session_id = _create(agent_env).json()["data"]["session_id"]

    first = _run(agent_env, session_id).json()["data"]
    assert first["status"] == "completed" and first["errors"] == []
    assert [t["status"] for t in first["tool_calls"]] == ["succeeded", "succeeded"]

    sink.clear()
    second = _run(agent_env, session_id, message="那我先改哪一段？").json()["data"]

    # 1) 请求结构
    assert [phase for phase, _ in sink] == ["select", "compose"], (
        f"一轮应当正好两次模型调用，实际 {[p for p, _ in sink]}"
    )
    for phase, msgs in sink:
        assert_protocol_clean(msgs, f"第二轮 {phase}")
    # 选工具轮确定性重建：System + Human，不带任何历史
    assert len(sink[0][1]) == 2

    # 2) 这一轮是完整的
    assert second["run_id"] != first["run_id"]
    assert second["status"] == "completed", second["errors"]
    assert second["errors"] == []
    assert [t["status"] for t in second["tool_calls"]] == ["succeeded", "succeeded"]
    assert second["output"]["evidence"], "第二轮必须带回证据（缺陷形态是零证据）"

    # 3) RunView 只含本次运行的调用（跨轮累积会变成 4 条）
    assert len(second["tool_calls"]) == 2, (
        f"RunView.tool_calls 应按基线切片，实际 {len(second['tool_calls'])} 条："
        f"{[t['tool'] for t in second['tool_calls']]}"
    )
    assert len(first["tool_calls"]) == 2


# ===========================================================================
# 工具目录
# ===========================================================================
def test_tools_endpoint_lists_all_catalog_tools_with_wire_names(agent_env):
    """``/tools`` 列出**完整目录**（含写工具），这一点与 ``build_model_tools`` 不同。

    两者的差别是**有意的**，也正是这轮 beta 要钉住的：

    - ``/tools`` 是给人/前端看的目录——必须能看到写工具及其确认要求；
    - ``build_model_tools`` 是给模型的 function-calling 清单——**只读**
      （9.2 节：写操作只能经 ActionIntent + 用户确认）。
    """
    data = agent_env.client.get(f"{BASE}/tools").json()["data"]
    by_name = {t["name"]: t for t in data}

    assert set(by_name) == {
        "resume.get", "jd.get", "match.get",
        "application.get", "application.update_status",
    }
    reads = {n for n, t in by_name.items() if t["kind"] == "read"}
    writes = {n for n, t in by_name.items() if t["kind"] == "write"}
    assert reads == {"resume.get", "jd.get", "match.get", "application.get"}
    assert writes == {"application.update_status"}
    # 模型侧看到的是线名（含点号会被 provider 400 拒绝）
    assert by_name["resume.get"]["wire_name"] == "resume_get"
    assert by_name["jd.get"]["wire_name"] == "jd_get"
    assert by_name["match.get"]["wire_name"] == "match_get"


# ===========================================================================
# API 层并发隔离：两个用户真正并发地跑各自的会话
# ===========================================================================
def test_concurrent_users_never_cross_sessions(agent_env, monkeypatch):
    """两个用户**同时**发请求：各自的会话、结论、证据都不许串。

    这条是把 graph 层的「依赖只能来自 per-run config」验收搬到 **API 层**：
    - 每次请求自己 ``make_deps(db, user.id)`` + ``graph_config(session_id, deps)``；
    - 没有任何模块级全局依赖，所以并发请求不存在"后到者覆盖先到者"的窗口。

    做法是用同一事件循环上的 ASGI 直连并发发两个请求（不是两次串行调用），
    否则测不出交错。
    """
    import asyncio

    import httpx

    served_users = []

    def _llm_for(user):
        served_users.append(user.id)
        own = {"u1": ("jd1", "r1"), "u2": ("jd2", "r2")}[user.id]
        return GoodLLM(jd_id=own[0], resume_id=own[1])

    monkeypatch.setattr(agent_routes, "get_user_llm_or_raise", _llm_for)

    # 两个用户各自的上下文（会话必须由**各自的用户**创建，否则 owner 就错了）
    def _create_as(user_id, jd_id, resume_id):
        return agent_env.client.post(
            f"{BASE}/sessions",
            json={"entry_source": "jd_page", "jd_id": jd_id, "resume_id": resume_id},
            headers={"x-test-user": user_id},
        ).json()["data"]["session_id"]

    s1 = _create_as("u1", "jd1", "r1")
    s2 = _create_as("u2", "jd2", "r2")
    assert s1 != s2

    async def _one(session_id, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"x-test-user": user_id},
        ) as ac:
            r = await ac.post(
                f"{BASE}/sessions/{session_id}/messages",
                json={"message": "我下一步该做什么？"},
            )
            return r.json()["data"]

    async def _both():
        return await asyncio.gather(_one(s1, "u1"), _one(s2, "u2"))

    with agent_env.Session() as probe:
        before = (
            probe.query(Resume).count(),
            probe.query(JobDescription).count(),
            probe.query(MatchResult).count(),
        )

    r1, r2 = asyncio.run(_both())

    with agent_env.Session() as probe:
        after = (
            probe.query(Resume).count(),
            probe.query(JobDescription).count(),
            probe.query(MatchResult).count(),
        )
    assert before == after, "并发运行同样不得写任何业务数据"

    # 各自拿自己的会话与 run
    assert r1["session_id"] == s1 and r2["session_id"] == s2
    assert r1["run_id"] != r2["run_id"]
    assert r1["status"] == r2["status"] == "completed"
    assert r1["errors"] == [] and r2["errors"] == []

    # 模型是按各自身份取的（没有"后到者覆盖先到者"）
    assert sorted(served_users) == ["u1", "u2"]

    # 证据互不污染：每条 ref 都来自**自己那次运行**的 call_id
    refs1 = {e["ref"] for e in r1["output"]["evidence"]}
    refs2 = {e["ref"] for e in r2["output"]["evidence"]}
    assert refs1 and refs2 and not (refs1 & refs2)

    # trace 也是各记各的；且互相看不见对方的会话
    t1 = agent_env.client.get(
        f"{BASE}/sessions/{s1}/trace", headers={"x-test-user": "u1"},
    ).json()["data"]
    t2 = agent_env.client.get(
        f"{BASE}/sessions/{s2}/trace", headers={"x-test-user": "u2"},
    ).json()["data"]
    assert [r["run_id"] for r in t1["runs"]] == [r1["run_id"]]
    assert [r["run_id"] for r in t2["runs"]] == [r2["run_id"]]
    assert agent_env.client.get(
        f"{BASE}/sessions/{s1}", headers={"x-test-user": "u2"},
    ).status_code == 404


def test_same_session_rejects_second_concurrent_message(agent_env, monkeypatch):
    """同一会话同一时刻只允许一条消息在执行（2.11 节）。"""
    import asyncio

    import httpx

    started = __import__("threading").Event()
    release = __import__("threading").Event()

    class SlowLLM(GoodLLM):
        def invoke(self, messages):
            started.set()
            release.wait(timeout=10)      # 卡住第一次运行，制造"正在执行"
            return super().invoke(messages)

    _use_llm(monkeypatch, SlowLLM)
    session_id = _create(agent_env).json()["data"]["session_id"]

    async def _scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"x-test-user": "u1"},
        ) as ac:
            first = asyncio.ensure_future(
                ac.post(f"{BASE}/sessions/{session_id}/messages",
                        json={"message": "第一次"}),
            )
            # 等第一次真正进入运行（模型已被调用）再发第二次
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.02)

            async def _release_soon():
                # 兜底：万一次请求没被 409 拦下而是排队等锁，也要能解锁收场，
                # 不能把测试挂死（宁可断言失败，不要 hang）。
                await asyncio.sleep(3)
                release.set()

            keeper = asyncio.ensure_future(_release_soon())
            try:
                second = await ac.post(
                    f"{BASE}/sessions/{session_id}/messages",
                    json={"message": "第二次"},
                )
            finally:
                release.set()
                keeper.cancel()
            return await first, second

    try:
        first, second = asyncio.run(_scenario())
    finally:
        release.set()

    assert first.status_code == 200
    assert second.status_code == 409
    assert "正在执行" in second.json()["detail"]


def test_model_cannot_read_another_users_objects(agent_env, monkeypatch):
    """模型即使硬编码别人的 jd/resume id，也读不到——网关按本人身份执行。

    这是把越权防线做在**身份注入层**（而不是靠模型自觉）的证明：
    模型看到的只是 id 字符串，身份由 ToolGateway 绑定，模型无从指定。
    """
    # GoodLLM 默认去读 jd1/r1（都属于 u1）
    _use_llm(monkeypatch, GoodLLM)

    session_id = agent_env.client.post(
        f"{BASE}/sessions",
        json={"entry_source": "jd_page", "jd_id": "jd2", "resume_id": "r2"},
        headers={"x-test-user": "u2"},
    ).json()["data"]["session_id"]

    data = agent_env.client.post(
        f"{BASE}/sessions/{session_id}/messages",
        json={"message": "帮我看看这个岗位"},
        headers={"x-test-user": "u2"},
    ).json()["data"]

    # 只读工具全部 not_found；一条证据都产不出来
    assert {t["tool"]: t["status"] for t in data["tool_calls"]} == {
        "jd.get": "not_found", "resume.get": "not_found",
    }
    assert data["output"]["evidence"] == []
    # 而且没有把 u1 的私有内容带出来
    blob = json.dumps(data, ensure_ascii=False)
    assert "A公司" not in blob
    assert "张三" not in blob
