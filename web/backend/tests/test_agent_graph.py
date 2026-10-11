"""Alpha graph 的执行边界回归测试。"""
import json
import threading
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.modules.career_agent.catalog import ToolResult
from app.modules.career_agent.graph import (
    _compose_payload,
    _raw_decision_text,
    build_alpha_graph,
    execute_read_tools,
    finalize,
    select_read_tools,
)
from app.modules.career_agent.schemas import AgentDecision, validate_evidence
from app.modules.career_agent.state import (
    ToolCallRecord,
    ToolLoopState,
    new_agent_state,
)


class _RecordingGateway:
    """记录工具是否发生并发调用以及调用了哪些线程。"""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.thread_ids = set()
        self._lock = threading.Lock()

    def invoke(self, tool_name, arguments):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.thread_ids.add(threading.get_ident())
        time.sleep(0.02)
        with self._lock:
            self.active -= 1
        return ToolResult(status="succeeded", tool_name=tool_name)


def test_alpha_tools_run_serially_on_the_calling_thread():
    """同一个 SQLAlchemy Session 不得被多个工具线程并发使用。"""
    gateway = _RecordingGateway()
    state = new_agent_state(thread_id="t1", run_id="r1", user_id="u1")
    state["tool_loop"] = ToolLoopState(calls=[
        ToolCallRecord(
            call_id="c1", tool_name="resume.get", status="pending",
            args_summary='{"resume_id": "r1"}',
        ),
        ToolCallRecord(
            call_id="c2", tool_name="jd.get", status="pending",
            args_summary='{"jd_id": "j1"}',
        ),
        ToolCallRecord(
            call_id="c3", tool_name="match.get", status="pending",
            args_summary='{"resume_id": "r1", "jd_id": "j1"}',
        ),
    ])

    result = execute_read_tools(state, {"deps": SimpleNamespace(gateway=gateway)})

    assert gateway.max_active == 1
    assert len(gateway.thread_ids) == 1
    assert [call.status for call in result["tool_loop"].calls] == [
        "succeeded", "succeeded", "succeeded",
    ]


# ---------------------------------------------------------------------------
# 第二次模型调用（compose）——设计文档 20.3 的 `summarize_tool_results → model`
# ---------------------------------------------------------------------------
def test_alpha_graph_has_second_model_call_before_validation():
    """设计文档 20.3 要求工具结果之后还有一次 `model` 才到 validate_output。

    回归闸门：缺这个节点时，没有任何东西产出 AgentDecision，校验只能去读
    上一条消息（上下文 JSON），报出一堆与真实原因无关的字段错误。
    """
    graph = build_alpha_graph()
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    nodes = set(graph.get_graph().nodes)

    assert {"select_read_tools", "compose_decision", "validate_output"} <= nodes
    assert ("summarize_tool_results", "compose_decision") in edges
    assert ("compose_decision", "validate_output") in edges
    # 不能再直接从工具结果跳到校验
    assert ("summarize_tool_results", "validate_output") not in edges


def test_raw_decision_text_reads_only_the_last_message():
    """校验只能认 compose 那条回答，不能向下回退到上下文 JSON。"""
    state = new_agent_state(thread_id="t", run_id="r", user_id="u1")

    # 模型只返回 tool_calls（content 为空）时，绝不能把上下文当结论
    state["messages"] = [
        SystemMessage(content="system"),
        HumanMessage(content='{"objects": [{"type": "jd"}]}'),
        AIMessage(content=""),
    ]
    assert _raw_decision_text(state) == ""

    state["messages"] = state["messages"] + [
        AIMessage(content='{"decision": "wait", "confidence": 0.5, "summary": "s"}')
    ]
    assert '"wait"' in _raw_decision_text(state)


def test_compose_payload_exposes_tool_results_and_allowed_refs():
    """compose 轮必须看得到每个工具的结果与唯一合法的证据引用集合。"""
    state = new_agent_state(thread_id="t", run_id="r", user_id="u1")
    state["input"].message = "我下一步该做什么？"
    state["tool_loop"] = ToolLoopState(calls=[
        ToolCallRecord(
            call_id="c1", tool_name="jd.get", status="succeeded",
            args_summary='{"jd_id": "j1"}',
            result_summary='{"status": "succeeded", "object_version": "jd:j1:v1"}',
        ),
        # 未注册工具从未执行，不得进模型视野
        ToolCallRecord(
            call_id="c2", tool_name="admin.delete_all", status="failed",
            error_code="tool_not_allowed",
        ),
    ])

    payload = _compose_payload(state)

    assert [c["tool"] for c in payload["tool_calls"]] == ["jd.get"]
    assert payload["tool_calls"][0]["result"].startswith('{"status"')
    assert payload["available_evidence_refs"] == []
    assert payload["missing_context"] == []


def test_finalize_does_not_launder_failure_into_completed():
    """`validate_output` 判失败后，finalize 不得把终态改回 completed。"""
    state = new_agent_state(thread_id="t", run_id="r", user_id="u1")
    state["run"].status = "failed"
    assert finalize(state)["run"].status == "failed"

    state["run"].status = "composing"
    assert finalize(state)["run"].status == "completed"


def test_evidence_is_rejected_when_no_tool_result_exists():
    """无工具结果 ⇒ 没有任何合法引用；此时任何 ref 都是编造的。"""
    decision = AgentDecision(
        decision="apply_now", confidence=0.9, summary="x",
        evidence=[{"ref": "jd.get:abc:result", "source": "jd.get"}],
    )
    report = validate_evidence(decision, allowed_refs=[])
    assert not report.ok
    assert "疑似伪造" in report.errors[0]

    # 真的没有证据、也没有编造时，应当放行（不能误伤）
    clean = AgentDecision(decision="wait", confidence=0.4, summary="缺上下文")
    assert validate_evidence(clean, allowed_refs=[]).ok


def test_execute_read_tools_records_bounded_result_summary():
    """每次调用的结果必须各自留下有界摘要，供 compose 轮读取。"""
    class _Gateway:
        def invoke(self, tool_name, arguments):
            return ToolResult(
                status="succeeded", tool_name=tool_name,
                summary="ok", object_version="v1",
                data={"blob": "x" * 5000},
            )

    state = new_agent_state(thread_id="t", run_id="r", user_id="u1")
    state["tool_loop"] = ToolLoopState(calls=[
        ToolCallRecord(call_id="c1", tool_name="resume.get", status="pending",
                       args_summary='{"resume_id": "r1"}'),
        ToolCallRecord(call_id="c2", tool_name="jd.get", status="pending",
                       args_summary='{"jd_id": "j1"}'),
    ])

    result = execute_read_tools(state, {"deps": SimpleNamespace(gateway=_Gateway())})
    records = result["tool_loop"].calls

    assert all(c.result_summary for c in records)
    assert all(len(c.result_summary) <= 2030 for c in records)
    assert all(c.status == "succeeded" for c in records)
    # 契约是 Optional[str]：塞 datetime 时 Pydantic 只在序列化时告警、不抛异常，
    # 单测不检查就会被漏过去（真模型运行的日志才暴露）。
    assert all(isinstance(c.ended_at, str) for c in records)


# ---------------------------------------------------------------------------
# 依赖注入：必须 per-run，不得有任何模块级全局（阶段 3 收口）
# ---------------------------------------------------------------------------
def test_deps_come_from_per_run_config_only():
    """依赖只认 per-run config；**没有全局兜底**——取不到就报错。

    早期实现用模块级 ``_ACTIVE_DEPS`` 兜底：单用户脚本看不出问题，
    但 API 并发下 A 写入的 deps 会被 B 覆盖，于是 A 的工具调用用 B 的
    user_id 执行（跨用户越权）。去掉兜底后"忘记注入"变成**立即报错**，
    而不是**静默串号**。
    """
    import pytest

    from app.modules.career_agent.graph import _deps_from, graph_config

    marker = SimpleNamespace(user_id="u1")

    with pytest.raises(RuntimeError):
        _deps_from(None)
    with pytest.raises(RuntimeError):
        _deps_from({})
    with pytest.raises(RuntimeError):
        _deps_from({"configurable": {}})

    # 标准位置：configurable.deps
    assert _deps_from({"configurable": {"deps": marker}}) is marker
    # 兼容单测直接给节点函数传 {"deps": ...}
    assert _deps_from({"deps": marker}) is marker

    # configurable 优先，且 thread_id 与 deps 必须同处 configurable
    other = SimpleNamespace(user_id="u2")
    conf = graph_config("th-1", marker)
    assert conf["configurable"]["thread_id"] == "th-1"
    assert conf["configurable"]["deps"] is marker
    assert _deps_from({"configurable": {"deps": marker}, "deps": other}) is marker
    assert _deps_from(conf) is marker


class _FakeDeps:
    """只为并发隔离测试存在的轻量 deps（gateway 是记录用的假实现）。"""

    def __init__(self, db, user_id, llm):
        self.db = db
        self.user_id = user_id
        self.llm = llm
        self.gateway = _OwnerRecordingGateway(user_id)


class _OwnerRecordingGateway:
    """记录"是谁在调用我"，用于证明两个并发运行没有交换依赖。"""

    def __init__(self, user_id):
        self.owner = user_id
        self.seen_owners = set()

    def build_model_tools(self):
        # 与真实网关一致：模型只看得见线名
        return [{"type": "function", "function": {"name": "jd_get", "parameters": {}}}]

    def invoke(self, tool_name, arguments):
        self.seen_owners.add(self.owner)
        return ToolResult(
            status="succeeded", tool_name=tool_name,
            object_version=f"{self.owner}:v1", summary=f"{self.owner} 的结果",
        )


class _TwoPhaseLLM:
    """两轮假模型：先选工具，再给结论。

    ⚠️ 工具名必须是 **provider 线名**（``jd_get``）——真实模型只看得见线名
    （假模型发契约名的教训见复盘文档第 10 章）。
    """

    def __init__(self, tag, delay=0.05):
        from langchain_core.messages import AIMessage
        self._AIMessage = AIMessage
        self._tag = tag
        self._delay = delay
        self._selecting = False

    def bind_tools(self, tools, **kwargs):
        self._selecting = True
        return self

    def invoke(self, messages):
        time.sleep(self._delay)          # 人为拉长，强制两路交错
        if self._selecting:
            self._selecting = False
            return self._AIMessage(content="", tool_calls=[{
                "name": "jd_get", "args": {}, "id": f"call_{self._tag}",
                "type": "tool_call",
            }])
        refs = _refs_from_compose_messages(messages)
        return self._AIMessage(content=json.dumps({
            "decision": "collect_evidence",
            "confidence": 0.5,
            "summary": f"{self._tag} 的结论",
            "actions": [],
            "evidence": [{"ref": r, "source": "jd.get"} for r in refs],
            "missing_info": [],
        }, ensure_ascii=False))


def _refs_from_compose_messages(messages):
    """从 compose 轮的**用户消息**里取出 available_evidence_refs（假模型照抄真实引用）。

    ⚠️ 只认 HumanMessage：``COMPOSE_PROMPT`` 自己就写着 ``available_evidence_refs``
    这个词（字段说明），按字符串遍历会先命中 System 提示词，
    再去解析提示词里的 JSON 片段 → JSONDecodeError。
    """
    for m in messages:
        if not isinstance(m, HumanMessage):
            continue
        content = getattr(m, "content", "") or ""
        if "available_evidence_refs" in content:
            raw = content[content.find("{"): content.rfind("}") + 1]
            return json.loads(raw).get("available_evidence_refs") or []
    return []


def _isolated_session():
    """一份**独占**的内存 SQLite 会话（StaticPool：线程池里的节点也能用）。

    LangGraph 会把同步节点丢到线程池执行，而 ``:memory:`` 默认是
    「每线程一条连接」⇒ 换线程就换了一个空库。StaticPool 全程复用同一条连接，
    ``run_evals.py`` 用的也是这一套。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.database import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_select_read_tools_never_replays_message_history(assert_protocol_clean):
    """选工具轮的请求必须**确定性重建**，不得续用 ``state["messages"]``。

    真实缺陷（2026-10-04 生产冒烟）：``select_read_tools`` 发的就是 ``state["messages"]``，
    而 ``execute_read_tools`` 的结果**从不写回 messages** ⇒ 历史里留下"带 ``tool_calls``
    却没有配对 tool 消息"的孤儿 AIMessage（外加每轮插进对话中间的 System）。
    同一会话**首轮必过、第二条消息起必 400**，表现为那一轮零工具零证据却仍返回
    ``completed``。``compose_decision`` 早已重建干净列表绕开此坑，选工具这一路没跟上。

    这里把线上第二轮的实际形态**人工灌进 State**，断言发出去的请求仍然干净。
    判据落在消息结构上：假模型不校验协议，靠"模型报不报错"抓不住。
    """
    import asyncio

    sent = []

    class _LLM:
        """只记录"模型真正收到了什么"。"""

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            sent.append(list(messages))
            return AIMessage(content="", tool_calls=[{
                "name": "jd_get", "args": {}, "id": "c1", "type": "tool_call",
            }])

    state = new_agent_state(thread_id="t", run_id="r", user_id="u1")
    state["messages"] = [
        SystemMessage(content="system"),
        HumanMessage(content='{"objects": []}'),
        # 孤儿：带 tool_calls 却没有任何配对的 tool 消息
        AIMessage(content="", tool_calls=[{
            "name": "jd_get", "args": {}, "id": "old1", "type": "tool_call",
        }]),
        AIMessage(content="上一轮的结论"),
        SystemMessage(content="system"),          # 中间插入的 System
        HumanMessage(content='{"objects": []}'),
    ]

    asyncio.run(select_read_tools(
        state, {"deps": SimpleNamespace(llm=_LLM(), gateway=_OwnerRecordingGateway("u1"))},
    ))

    assert len(sent) == 1, "选工具轮只应产生一次模型调用"
    assert_protocol_clean(sent[0], "select")
    # 确定性重建：只有 System + Human，历史一条都不带
    assert len(sent[0]) == 2, f"选工具轮不该携带历史，实际 {len(sent[0])} 条"
    assert isinstance(sent[0][0], SystemMessage)
    assert isinstance(sent[0][1], HumanMessage)


def test_protocol_judge_itself_fires(assert_protocol_clean):
    """判据自己的反向对照：非法形态喂进去，它必须都报错。

    反向对照也会**静默失效**——判据写错方向就永远绿，"全绿"与"没测到"分不清。
    所以先把各种非法形态直接喂给判据，确认它会响（三种孤儿形态分别覆盖三个分支）。
    """
    import pytest

    from langchain_core.messages import ToolMessage

    _ORPHAN = AIMessage(content="", tool_calls=[
        {"name": "jd_get", "args": {}, "id": "c1", "type": "tool_call"}])

    assert_protocol_clean(
        [SystemMessage(content="s"), HumanMessage(content="h")], "合法样本",
    )   # 合法的不许误伤
    assert_protocol_clean(   # 配对的 tool 消息不算孤儿
        [SystemMessage(content="s"), HumanMessage(content="h"), _ORPHAN,
         ToolMessage(content="{}", tool_call_id="c1")],
        "合法配对",
    )

    # 分支 1：孤儿之后跟着的是别的消息（不是配对的 tool 消息）——线上第二轮的形态
    with pytest.raises(AssertionError, match="不是 tool 消息"):
        assert_protocol_clean([
            SystemMessage(content="s"), HumanMessage(content="h"),
            _ORPHAN, AIMessage(content="结论"),
            SystemMessage(content="s2"), HumanMessage(content="h2"),
        ], "孤儿 tool_calls（后面跟着别的东西）")

    # 分支 2：孤儿就是最后一条，后面什么都没有
    with pytest.raises(AssertionError, match="一条消息都没有"):
        assert_protocol_clean([
            SystemMessage(content="s"), HumanMessage(content="h"),
            _ORPHAN,
        ], "孤儿 tool_calls（末尾）")

    # 分支 3：tool_call_id 不配对
    with pytest.raises(AssertionError, match="不配对"):
        assert_protocol_clean([
            SystemMessage(content="s"), HumanMessage(content="h"),
            _ORPHAN, ToolMessage(content="{}", tool_call_id="c9"),
        ], "tool_call_id 不配对")

    with pytest.raises(AssertionError, match="System 消息出现在第 2 位"):
        assert_protocol_clean([
            SystemMessage(content="s"), HumanMessage(content="h"),
            SystemMessage(content="s2"),
        ], "中间插 System")

    with pytest.raises(AssertionError, match="一条消息都没有"):
        assert_protocol_clean([], "空请求")

    with pytest.raises(AssertionError, match="不是消息对象"):
        assert_protocol_clean([{"role": "user", "content": "h"}], "dict 混入")


def test_concurrent_runs_do_not_share_deps():
    """两个**并发**运行必须各用各的依赖，一个都不能串。

    这是"模块级全局注入"的真实故障形态：全局只存一份 deps，
    并发时后到的请求覆盖先到的，先到的请求随后用别人的身份执行工具。
    依赖改走 per-run config 后，串号在结构上不可能发生。

    ⚠️ 两个运行给**各自独立的 db 会话**：真实服务里每个请求也各有自己的
    Session（FastAPI 依赖注入按请求建），共用一个 Session 本身就是非法用法。
    """
    import asyncio

    from app.modules.career_agent.graph import graph_config

    graph = build_alpha_graph()
    db_a, db_b = _isolated_session(), _isolated_session()
    deps_a = _FakeDeps(db_a, "u1", _TwoPhaseLLM("a"))
    deps_b = _FakeDeps(db_b, "u2", _TwoPhaseLLM("b"))

    async def _one(deps, tag):
        st = new_agent_state(thread_id=f"th-{tag}", run_id=f"run-{tag}", user_id=deps.user_id)
        st["input"].message = "我下一步该做什么？"
        st["input"].entry_source = "test"
        return await graph.ainvoke(st, config=graph_config(f"th-{tag}", deps))

    async def _both():
        return await asyncio.gather(_one(deps_a, "a"), _one(deps_b, "b"))

    try:
        final_a, final_b = asyncio.run(_both())
    finally:
        db_a.close()
        db_b.close()

    # 每个运行自己的网关只被自己的依赖调用过
    assert deps_a.gateway.seen_owners == {"u1"}
    assert deps_b.gateway.seen_owners == {"u2"}
    # 状态里的身份也没被换掉
    assert final_a["run"].user_id == "u1"
    assert final_b["run"].user_id == "u2"
    # 两条运行的 call_id（uuid4）与证据不互相污染
    refs_a = {e.ref for e in final_a["decision"].evidence}
    refs_b = {e.ref for e in final_b["decision"].evidence}
    assert refs_a and refs_b
    assert not (refs_a & refs_b)
    assert all(r.startswith("jd.get:") for r in refs_a | refs_b)
    # 结论各自带自己的 tag，证明没有交叉
    assert "a 的结论" in final_a["output"].message
    assert "b 的结论" in final_b["output"].message


def test_summarize_reads_evidence_sink_from_config(db_session):
    """证据回调（评测挂点）同样只能从 per-run config 取，不能读全局。"""
    from app.modules.career_agent.graph import summarize_tool_results

    seen = []

    class _Deps:
        def evidence_sink(self, refs):
            seen.extend(refs)

    state = new_agent_state(thread_id="t", run_id="r", user_id="u1")
    state["tool_loop"] = ToolLoopState(calls=[ToolCallRecord(
        call_id="c1", tool_name="jd.get", status="succeeded",
        result_summary='{"status": "succeeded"}',
    )])

    out = summarize_tool_results(state, {"configurable": {"deps": _Deps()}})
    assert [e.ref for e in out["decision"].evidence] == ["jd.get:c1:result"]
    assert seen == ["jd.get:c1:result"]

    # 无 config 时不报错、也不回调（生产依赖本来就没有 sink）
    out2 = summarize_tool_results(state)
    assert [e.ref for e in out2["decision"].evidence] == ["jd.get:c1:result"]
