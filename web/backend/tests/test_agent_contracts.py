"""Agent 契约层单测（AgentStateV1 + ToolCatalog v1）。

对应设计文档第 22.1 节「阶段 0：契约」——**任何契约测试失败都禁止进入下一阶段**。

重点覆盖三类红线：
1. State 不得出现 ORM / Session / 密钥 / 完整正文。
2. 工具约束（domain.action、写工具三必须、状态枚举）必须**真的拦住**，
   防回归到 `__post_init__` 在 Pydantic v2 上静默失效的坑。
3. `user_id` 只能由运行时注入，模型 schema 不得暴露。
"""
import pytest
from pydantic import BaseModel, ValidationError
from typing import get_type_hints

from app.modules.career_agent.catalog import (
    CATALOG,
    TOOL_STATUSES,
    WIRE_NAME_PATTERN,
    WIRE_TO_CANONICAL,
    ToolGateway,
    ToolResult,
    ToolSpec,
    get_tool,
    list_tools,
    resolve_tool_name,
    to_wire_name,
)
from app.modules.career_agent.state import (
    SCHEMA_VERSION,
    TERMINAL_STATUSES,
    AgentStateV1,
    new_agent_state,
)


# ---------------------------------------------------------------------------
# State：结构与不变量
# ---------------------------------------------------------------------------
def test_new_state_defaults():
    s = new_agent_state(thread_id="t1", run_id="r1", user_id="u1")
    assert s["run"].status == "created"
    assert s["schema_version"] == SCHEMA_VERSION
    # alpha 上限（设计文档 20.2）
    assert s["limits"].max_model_turns == 4
    assert s["limits"].max_tool_calls == 3
    # alpha 不做写操作与长期记忆
    assert s["approval"] is None
    assert s["memory"].proposed == []


def test_state_is_json_serializable():
    """Checkpoint 前提：整个状态必须可 JSON 序列化。"""
    import json
    s = new_agent_state("t", "r", "u")
    payload = {k: v for k, v in s.items() if hasattr(v, "model_dump")}
    json.dumps({k: v.model_dump(mode="json") for k, v in payload.items()}, ensure_ascii=False)


def test_state_has_no_forbidden_objects():
    """State 里不得出现 ORM / Session / 凭据 / 完整正文。

    ⚠️ 用 **AST 检查真实导入与注解**，不要用 `'Session' in src` 扫源码文本——
    注释里写着「禁止出现 SQLAlchemy Session」也会被匹配上，导致假失败。
    （本项目已实测踩过这个坑。）
    """
    import ast
    import typing
    from app.modules.career_agent import state as state_mod

    tree = ast.parse(open(state_mod.__file__, encoding="utf-8").read())

    # 1) 不得真实 import SQLAlchemy
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("sqlalchemy" in m for m in imported), f"不得导入 SQLAlchemy：{imported}"

    # 2) 不得真实使用 Session / ORM 类做注解
    annotations: typing.List[str] = []

    def _collect(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.AnnAssign) and sub.annotation:
                annotations.append(ast.unparse(sub.annotation))
            if isinstance(sub, ast.arg) and sub.annotation:
                annotations.append(ast.unparse(sub.annotation))
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.returns:
                annotations.append(ast.unparse(sub.returns))

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect(node)

    joined = " ".join(annotations)
    for banned in ("Session", "DeclarativeBase", "BaseModelORM"):
        assert banned not in joined, f"注解里不得出现 {banned}：{joined[:200]}"

    # 3) 不得有承载完整正文的字段。
    #    同理用精确名，避免 `preview` 之类的合法短字段被误伤。
    text_fields = {"raw_text", "full_text", "resume_body", "jd_body", "content", "text"}
    for a in annotations:
        name = a.split(":", 1)[0].strip().rsplit(".", 1)[-1]
        assert name not in text_fields, f"不得保存完整正文字段：{a}"

    # 4) 凭据类字段一律禁止。
    #    注意：用「精确字段名」匹配，不能用子串——`TokenUsage` 是合法的用量统计，
    #    用 `"token" in annotation` 会把它误判为凭据（已实测踩过）。
    for field in ("api_key", "access_token", "password", "secret_key"):
        assert field not in annotations, f"不得保存凭据字段：{field}"


def test_reducers_present_and_merging():
    """reducer 必须真实挂上且能合并（Checkpoint 增量更新依赖）。"""
    import operator
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph.message import add_messages

    hints = get_type_hints(AgentStateV1, include_extras=True)
    assert hints["messages"].__metadata__      # 有 reducer
    assert hints["errors"].__metadata__        # 有 reducer

    merged = add_messages([HumanMessage(content="a")], [AIMessage(content="b")])
    assert len(merged) == 2
    assert len(operator.add([{"code": "A"}], [{"code": "B"}])) == 2


def test_terminal_statuses_cover_failure_paths():
    assert {"completed", "failed", "cancelled"} <= TERMINAL_STATUSES
    # planning / tool_running 必须不是终态（设计文档 12.3）
    assert "planning" not in TERMINAL_STATUSES
    assert "tool_running" not in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# ToolSpec 约束（防 __post_init__ 回归）
# ---------------------------------------------------------------------------
class _Dummy(BaseModel):
    pass


def test_tool_name_must_be_domain_action():
    with pytest.raises(ValidationError):
        ToolSpec(name="nodot", description="x", kind="read",
                 input_model=_Dummy, output_model=ToolResult)


@pytest.mark.parametrize("kwargs,expect", [
    ({"side_effect": "none"}, "side_effect"),
    ({"side_effect": "write", "confirmation_required": False}, "confirmation_required"),
    ({"side_effect": "write", "confirmation_required": True,
      "idempotency_required": False}, "idempotency_required"),
])
def test_write_tool_invariants(kwargs, expect):
    """写工具必须同时满足三约束：side_effect / confirmation / idempotency。"""
    params = dict(
        name="application.update_status", description="x", kind="write",
        input_model=_Dummy, output_model=ToolResult,
    )
    params.update(kwargs)
    with pytest.raises(ValidationError) as err:
        ToolSpec(**params)
    assert expect in str(err.value)


def test_write_tool_valid_passes():
    """合法的写工具应通过（防止约束过严误伤）。"""
    spec = ToolSpec(
        name="application.update_status", description="x", kind="write",
        input_model=_Dummy, output_model=ToolResult,
        side_effect="write", confirmation_required=True, idempotency_required=True,
    )
    assert spec.kind == "write"


def test_tool_result_rejects_unknown_status():
    """ToolResult 状态只允许 10 个定义值。"""
    with pytest.raises(ValidationError):
        ToolResult(status="whatever", tool_name="x.y")
    # 且枚举本身与设计文档一致
    assert TOOL_STATUSES == {
        "succeeded", "empty", "needs_input", "forbidden", "not_found",
        "stale", "conflict", "rate_limited", "timeout", "failed",
    }


# ---------------------------------------------------------------------------
# ToolCatalog 注册与 schema
# ---------------------------------------------------------------------------
def test_catalog_registers_beta_tools():
    """目录内容与设计文档 5.1 / 5.2 对齐：只读工具 + 首个写入工具。

    beta 相对 alpha 多了 ``application.get``（读）与
    ``application.update_status``（写）。写入工具的**三条硬约束**
    （9.1 / 13.1：side_effect / confirmation / idempotency）在这里正面验证——
    它们由 ``ToolSpec`` 的 ``model_validator`` 强制，绕不过去。
    """
    assert sorted(CATALOG.keys()) == [
        "application.get",
        "application.update_status",
        "jd.get",
        "match.get",
        "resume.get",
    ]

    read_tools = [s for s in CATALOG.values() if s.kind == "read"]
    write_tools = [s for s in CATALOG.values() if s.kind == "write"]
    assert sorted(s.name for s in read_tools) == [
        "application.get", "jd.get", "match.get", "resume.get",
    ]
    assert all(s.side_effect == "none" for s in read_tools)

    assert [s.name for s in write_tools] == ["application.update_status"]
    for spec in write_tools:
        assert spec.side_effect == "write", "写工具必须显式声明副作用"
        assert spec.confirmation_required is True, "写工具必须要求用户确认"
        assert spec.idempotency_required is True, "写工具必须有幂等键"


def test_model_tool_schema_hides_identity(db_session):
    """模型可见的 schema 绝不能含 user_id / db——否则模型可指定身份。"""
    import json
    gw = ToolGateway.__new__(ToolGateway)
    blob = json.dumps(gw.build_model_tools(), ensure_ascii=False)
    assert "user_id" not in blob
    assert '"db"' not in blob


def test_get_tool_rejects_unknown():
    with pytest.raises(KeyError):
        get_tool("does.not.exist")


# ---------------------------------------------------------------------------
# 契约名 ↔ provider 线名
# ---------------------------------------------------------------------------
def test_wire_names_match_provider_pattern():
    """`tools[].function.name` 必须匹配 ^[a-zA-Z0-9_-]{1,64}$。

    回归闸门：2026-10-03 真模型验证 0/4 的**直接根因**就是契约名 `resume.get`
    含点号，被 DeepSeek 用 HTTP 400 拒绝：
        Invalid 'tools[0].function.name': string does not match pattern
    """
    for spec in list_tools():
        assert WIRE_NAME_PATTERN.match(spec.wire_name), f"{spec.name} -> {spec.wire_name}"
        assert len(spec.wire_name) <= 64


def test_build_model_tools_exposes_only_legal_wire_names():
    """模型实际收到的名字必须全都合法——这是那次 400 的正面证据。

    ⚠️ 同时钉住一条安全约束：**写工具绝不能出现在模型的 function-calling
    清单里**（设计文档 9.2：写操作必须先生成 ActionIntent 再等用户确认）。
    如果 ``build_model_tools`` 忘了按 ``kind`` 过滤，模型就能"直接调用"写工具，
    这条断言会立刻变红。
    """
    gw = ToolGateway.__new__(ToolGateway)
    names = sorted(t["function"]["name"] for t in gw.build_model_tools())
    for tool in gw.build_model_tools():
        name = tool["function"]["name"]
        assert WIRE_NAME_PATTERN.match(name), name
        assert "." not in name
    assert names == ["application_get", "jd_get", "match_get", "resume_get"]
    assert "application_update_status" not in names, "写工具不得暴露给模型"


def test_wire_name_mapping_is_bijective_and_reversible():
    """线名 ↔ 契约名必须一一对应，且双向都能解析。

    冲突会让模型的调用语义歧义（调 `a_b` 到底是 `a.b` 还是 `a_b`），
    因此 `_build_wire_map` 在构造期就会抛错——这里正面验证可逆性。
    """
    wires = [s.wire_name for s in CATALOG.values()]
    assert len(wires) == len(set(wires)), "线名出现冲突"

    for canonical, spec in CATALOG.items():
        assert resolve_tool_name(spec.wire_name) == canonical
        assert resolve_tool_name(canonical) == canonical
        assert to_wire_name(canonical) == spec.wire_name


def test_resolve_tool_name_does_not_fuzzy_match():
    """未注册的名字原样返回，绝不猜一个近似的已注册工具。

    否则模型（或被注入的 JD 内容）可以用一个错名字摸到别的工具。
    """
    assert resolve_tool_name("resume_delete") == "resume_delete"
    assert resolve_tool_name("admin.delete_all") == "admin.delete_all"
    assert resolve_tool_name("resume.get.delete") == "resume.get.delete"


def test_spec_with_illegal_wire_name_is_rejected():
    """放一个线名非法的工具进目录就该在**构造期**炸掉，而不是线上 400。"""
    with pytest.raises(ValidationError):
        ToolSpec(name="简历.读取", description="x", kind="read",
                 input_model=_Dummy, output_model=ToolResult)


def test_wire_to_canonical_contains_both_directions():
    for canonical, spec in CATALOG.items():
        assert WIRE_TO_CANONICAL[spec.wire_name] == canonical
        assert WIRE_TO_CANONICAL[canonical] == canonical


# ---------------------------------------------------------------------------
# Gateway 端到端（用 db_session fixture）
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded(db_session):
    from app.models.jd import JobDescription
    from app.models.resume import Resume
    from app.models.user import User

    db_session.add(User(id="u1", name="甲"))
    db_session.add(User(id="u2", name="乙"))
    db_session.add(JobDescription(
        id="jd1", user_id="u1", raw_text="Python 后端开发",
        company="A公司", position="后端工程师", must_have=["Python"],
    ))
    db_session.add(Resume(
        id="r1", user_id="u1", version=1,
        raw_text="张三 后端工程师 " * 40,
    ))
    db_session.commit()
    return db_session


def test_gateway_success_paths(seeded):
    gw = ToolGateway(seeded, user_id="u1")
    r = gw.invoke("resume.get", {"resume_id": "r1"})
    assert r.status == "succeeded"
    assert "raw_text" not in str(r.data)
    assert r.object_version and "v1" in r.object_version

    j = gw.invoke("jd.get", {"jd_id": "jd1"})
    assert j.status == "succeeded"
    assert "A公司" in j.summary


def test_gateway_match_not_found_no_recompute(seeded):
    """无匹配记录时返回 not_found，且不触发重算。"""
    from app.models.match import MatchResult
    gw = ToolGateway(seeded, user_id="u1")
    r = gw.invoke("match.get", {"resume_id": "r1", "jd_id": "jd1"})
    assert r.status == "not_found"
    assert r.error_code == "no_match_result"
    # 确认真的没写入任何 MatchResult
    assert seeded.query(MatchResult).count() == 0


def test_gateway_blocks_cross_user(seeded):
    """越权读他人简历必须 not_found。"""
    gw = ToolGateway(seeded, user_id="u2")
    r = gw.invoke("resume.get", {"resume_id": "r1"})
    assert r.status == "not_found"
    assert "张三" not in r.summary


def test_gateway_structured_errors_no_traceback(seeded):
    """未知工具 / 缺参数都返回结构化错误，且摘要不含异常栈。"""
    gw = ToolGateway(seeded, user_id="u1")

    r1 = gw.invoke("no.such.tool", {})
    assert r1.status == "failed"
    assert r1.error_code == "unknown_tool"

    r2 = gw.invoke("match.get", {"resume_id": "r1"})  # 缺 jd_id
    assert r2.status == "failed"
    assert r2.error_code == "invalid_arguments"
    assert "Traceback" not in r2.summary
    assert "Traceback" not in str(r2.data)


def test_gateway_identity_cannot_be_overridden(seeded):
    """模型伪造 user_id 参数无效——真实身份来自网关。"""
    gw = ToolGateway(seeded, user_id="u1")
    r = gw.invoke("resume.get", {"resume_id": "r1", "user_id": "u2"})
    # 即便多传字段，读取的仍是 u1 的数据
    assert r.status in ("succeeded", "failed")
    if r.status == "succeeded":
        assert r.data["resume"]["id"] == "r1"


# ---------------------------------------------------------------------------
# 网关入口的线名归一（阶段 3 收口）
# ---------------------------------------------------------------------------
def test_gateway_accepts_provider_wire_name(seeded):
    """入口归一：直接传 provider 线名也能正确执行，信封恒用契约名。

    回归闸门：真实模型只能看到 ``build_model_tools()`` 给出的线名
    （``resume_get``）。任何"照抄模型输出"调用网关的路径（Trace 重放、脚本、
    未来的确认写入）都必须能用线名跑通——不能因为一个点号就整条失败。
    """
    gw = ToolGateway(seeded, user_id="u1")
    r = gw.invoke("resume_get", {"resume_id": "r1"})

    assert r.status == "succeeded"
    assert r.tool_name == "resume.get"                  # 信封恒为契约名
    assert r.evidence_ref and r.evidence_ref.startswith("resume.get:")


def test_gateway_wire_and_canonical_names_are_equivalent(seeded):
    """线名与契约名必须走同一条路径、返回同一份数据（不是两套实现）。"""
    gw = ToolGateway(seeded, user_id="u1")
    canonical = gw.invoke("jd.get", {"jd_id": "jd1"})
    wire = gw.invoke("jd_get", {"jd_id": "jd1"})

    assert canonical.status == wire.status == "succeeded"
    assert canonical.tool_name == wire.tool_name == "jd.get"
    assert canonical.data == wire.data
    assert canonical.summary == wire.summary


def test_gateway_wire_name_cannot_bypass_identity(seeded):
    """归一不得绕过身份绑定：u2 用线名读 u1 的简历仍然 not_found。"""
    gw = ToolGateway(seeded, user_id="u2")
    r = gw.invoke("resume_get", {"resume_id": "r1"})

    assert r.status == "not_found"
    assert r.tool_name == "resume.get"
    assert "张三" not in r.summary


def test_gateway_wire_name_keeps_argument_validation(seeded):
    """归一后仍要过 Pydantic 参数校验，且错误信封用契约名。"""
    gw = ToolGateway(seeded, user_id="u1")
    r = gw.invoke("match_get", {"resume_id": "r1"})      # 缺 jd_id

    assert r.status == "failed"
    assert r.error_code == "invalid_arguments"
    assert r.tool_name == "match.get"


def test_gateway_rejects_unknown_name_without_fuzzy_match(seeded):
    """未注册的名字**不做模糊匹配**，一律 unknown_tool。

    否则模型（或被注入的 JD）可以拿一个"很像"的名字摸到别的工具。
    """
    gw = ToolGateway(seeded, user_id="u1")
    for bad in ("resume", "resume_getx", "resume.get.more", "admin.delete_all"):
        r = gw.invoke(bad, {"resume_id": "r1"})
        assert r.status == "failed", bad
        assert r.error_code == "unknown_tool", bad
        assert r.tool_name == bad, bad
