"""alpha 评测执行器：用**假模型 + 构造数据**跑 18 个固定用例。

设计依据：设计文档 22.1（分阶段验收）、22.3（评测集）、22.4（通过阈值）。

为什么用假模型：
- 平台额度 50000 token/UTC 日 ≈ 只够 2 次真模型简历生成；
- 评测要**确定性可重复**，不能受模型随机性影响。

假模型的行为**按用例期望编排**：它会像真实模型那样"选工具 + 输出 JSON"，
但选择与输出由用例的 ``expect_*`` 决定——这样测的是**我们的管道**
（上下文组装 → 工具执行 → 证据校验 → 输出校验）是否正确，
而不是测某个模型的随机表现。

用法：
    cd /app/backend && python -m app.modules.career_agent.evals.run_evals
"""
from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.jd import JobDescription
from app.models.match import MatchResult
from app.models.resume import Resume
from app.models.user import User
from app.modules.career_agent.evals.cases import EvalCase, build_cases
from app.modules.career_agent.graph import build_alpha_graph
from app.modules.career_agent.state import ContextRef, new_agent_state

VERBOSE = "-v" in sys.argv

#: 本次运行的真实证据 ref（由 graph 执行后回填给假模型）。
#: 用 dict 而非全局变量，便于并发与清理。
_EVIDENCE_HINT: Dict[str, List[str]] = {"refs": []}


# ===========================================================================
# 假模型
# ===========================================================================
class FakeLLM:
    """按用例期望编排的假模型。

    ⚠️ 必须返回**真正的 LangChain 消息对象**：``messages`` 字段挂的是
    ``add_messages`` reducer，它会做消息类型强制转换——返回自定义对象会报
    ``NotImplementedError: Unsupported message type``（本项目已实测踩过）。
    """
    def __init__(self, case: EvalCase, calls: List[Dict[str, Any]]):
        from langchain_core.messages import AIMessage
        self._AIMessage = AIMessage
        self._case = case
        self._calls = calls
        self._done_tools = False
        self._bound = False

    def bind_tools(self, tools, **kwargs):
        """langchain 标准用法是 bind_tools 后再 invoke。

        真实 ``ChatOpenAI`` 必须走这一步；直接给 ``invoke`` 传 ``tools=``
        会 ``KeyError``（本项目已实测踩过）。假模型只需记住"已进入选工具轮"。
        """
        self._bound = True
        return self

    def invoke(self, messages, tools=None, **kwargs):
        # 已 bind_tools → 这是选工具轮
        if self._bound and not self._done_tools:
            self._done_tools = True
            return self._AIMessage(content="", tool_calls=self._tool_calls())
        # 之后返回最终结构化输出
        return self._AIMessage(content=self._render_decision())

    def _tool_calls(self) -> List[Dict[str, Any]]:
        """构造 LangChain 要求的 tool_call 结构。

        ⚠️ 三个必须注意的点（本项目均实测踩过）：
        1. 每项含**必填的 ``id``**，缺了报 ``tool_call() missing 1 required
           keyword-only argument: 'id'``。
        2. 键名是 LangChain 原生的 ``name`` / ``args``（不是我们内部的
           ``tool_name`` / ``arguments``），否则取键会 ``KeyError``。
        3. ``name`` 必须用 **provider 线名**（``resume_get``）：真实模型只能看到
           ``build_model_tools()`` 给出的名字，而含点号的契约名会被
           DeepSeek/OpenAI 直接 400 拒绝。假模型必须模拟**真实模型看得见的东西**，
           否则线名映射这条路径永远测不到（真模型验证 0/4 的根因正是它）。
           未注册的工具名不做转换——真实模型就是照着自己编的名字调用的。
        """
        from app.modules.career_agent.catalog import CATALOG, to_wire_name

        out = []
        for i, c in enumerate(self._calls):
            raw = c["name"]
            name = to_wire_name(raw) if raw in CATALOG else raw
            out.append({
                "name": name,
                "args": c.get("args", {}),
                "id": f"call_{i}_{name}",
                "type": "tool_call",
            })
        return out

    def _render_decision(self) -> str:
        """按用例期望生成最终 JSON（模拟模型依据工具结果作答）。"""
        case = self._case
        decision = case.expect_decision or "collect_evidence"
        # 证据 ref 必须与 summarize_tool_results 生成的一致：{tool}:{call_id}:result
        # 但 call_id 在运行时才生成，因此这里用真实工具调用记录来取（见 _evidence_hint）
        refs = _evidence_hint()
        actions = []
        if case.expect_decision == "optimize_first":
            actions.append({
                "type": "optimize_resume",
                "title": "补充缺失技能的项目证据",
                "priority": 1,
                "reason": "目标 JD 存在硬缺口。",
                "evidence_refs": refs[:1],
                "requires_confirmation": True,
            })
        elif case.expect_decision == "apply_now":
            actions.append({
                "type": "apply_now",
                "title": "直接投递该岗位",
                "priority": 1,
                "reason": "匹配分高且无硬缺口。",
                "evidence_refs": refs[:1],
                "requires_confirmation": True,
            })
        payload = {
            "decision": decision,
            "confidence": 0.8,
            "summary": f"[{case.category}] 构造用例输出。",
            "actions": actions,
            "evidence": [
                {"ref": r, "source": "match.get", "object_version": "test", "summary": "构造证据"}
                for r in refs
            ],
            "missing_info": _missing_info(case),
        }
        return json.dumps(payload, ensure_ascii=False)


def _evidence_hint() -> List[str]:
    """取本次运行的**真实**证据 ref。

    假模型必须引用真实工具调用产生的 ref，否则会被 ``validate_evidence``
    判为「伪造证据」——这正是我们要测的能力，不能靠放宽校验来通过。
    """
    return list(_EVIDENCE_HINT["refs"])


def _missing_info(case: EvalCase) -> List[str]:
    if case.expect_missing:
        return [f"缺少 {case.expect_missing}，请先在页面上选择。"]
    return []


def _make_tool_calls(case: EvalCase) -> List[Dict[str, Any]]:
    """按用例构造工具选择。"""
    calls: List[Dict[str, Any]] = []
    jd_id = case.refs.get("jd_id")
    rs_id = case.refs.get("resume_id")
    if rs_id:
        calls.append({"name": "resume.get", "args": {"resume_id": rs_id}})
    if jd_id:
        calls.append({"name": "jd.get", "args": {"jd_id": jd_id}})
    if rs_id and jd_id and not any(f.startswith("missing_") or f == "stale_match" for f in (case.case_id,)):
        calls.append({"name": "match.get", "args": {"resume_id": rs_id, "jd_id": jd_id}})
    # 注入用例：额外让假模型尝试调用一个未注册工具，验证被拒
    if case.case_id == "jd_injection":
        calls.append({"name": "admin.delete_all", "args": {}})
    return calls


# ===========================================================================
# 数据装载
# ===========================================================================
def seed_case(db, case: EvalCase) -> None:
    f = case.fixture
    for uid in ("u1", "u2"):
        if not db.query(User).filter(User.id == uid).first():
            db.add(User(id=uid, name=f"用户{uid}"))
    db.commit()

    for key, model, extra in (
        ("jd", JobDescription, ("must_have", "nice_to_have", "tech_stack", "hidden_signals", "embedding_id")),
        ("jd2", JobDescription, ("must_have", "nice_to_have", "tech_stack", "hidden_signals", "embedding_id")),
        ("resume", Resume, ("version", "parsed_json", "source_file")),
        ("resume2", Resume, ("version", "parsed_json", "source_file")),
    ):
        data = f.get(key)
        if not data:
            continue
        obj = model(id=data["id"], user_id=data["user_id"], raw_text=data["raw_text"])
        for field_name in extra:
            if field_name in data:
                setattr(obj, field_name, data[field_name])
        db.add(obj)

    for key in ("resume_other",):
        data = f.get(key)
        if data:
            db.add(Resume(id=data["id"], user_id=data["user_id"], version=data.get("version", 1),
                          raw_text=data["raw_text"]))
    data = f.get("jd_other")
    if data:
        db.add(JobDescription(id=data["id"], user_id=data["user_id"], raw_text=data["raw_text"],
                              company=data.get("company"), position=data.get("position"),
                              must_have=data.get("must_have")))

    m = f.get("match")
    if m:
        db.add(MatchResult(
            id=m["id"], jd_id=m["jd_id"], resume_id=m["resume_id"], score=m["score"],
            skill_match_detail=m["skill_match_detail"], gap_analysis=m["gap_analysis"],
        ))
    db.commit()


# ===========================================================================
# 跑一个用例
# ===========================================================================
def run_case(case: EvalCase) -> Dict[str, Any]:
    _EVIDENCE_HINT["refs"] = []          # 防止用例间证据污染
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    seed_case(db, case)

    refs: List[ContextRef] = []
    if case.refs.get("jd_id"):
        refs.append(ContextRef(type="jd", id=case.refs["jd_id"]))
    if case.refs.get("resume_id"):
        refs.append(ContextRef(type="resume", id=case.refs["resume_id"]))

    calls = _make_tool_calls(case)
    llm = FakeLLM(case, calls)
    graph = build_alpha_graph()
    thread_id = f"eval-{case.case_id}"

    state = new_agent_state(thread_id=thread_id, run_id=str(uuid.uuid4()), user_id="u1")
    state["input"].message = "我该做什么？"
    state["input"].entry_source = "eval"
    state["input"].context_refs = refs

    # 假模型需要知道真实证据 ref（模拟模型依据工具返回作答）。
    # ⚠️ 证据 ref 由 graph 在 summarize_tool_results 阶段生成（含随机 call_id），
    #    所以要在**图执行过程中**回填给 deps，假模型才能引用真实 ref。
    # 依赖通过 **per-run config** 注入（设计文档 14.4），不再用任何全局。
    from app.modules.career_agent.graph import graph_config

    deps = _Deps(db, llm, case)
    config = graph_config(thread_id, deps)

    # ⚠️ 必须用 ainvoke：图里有 async 节点（select_read_tools / validate_output_node），
    # 同步 invoke() 会报 "No synchronous function provided"。
    # 这也正是设计文档 14.4「Agent 必须异步调用」要求的结果。
    import asyncio

    async def _go():
        return await graph.ainvoke(state, config=config)

    result_state = asyncio.run(_go())

    missing = [m.type for m in result_state["context"].missing_context]
    decision = result_state["decision"]
    output = result_state["output"]
    tool_calls = result_state["tool_loop"].calls
    errors = [e.message for e in result_state["errors"]]

    return {
        "case": case,
        "missing": missing,
        "decision": decision.intent if decision else None,
        "confidence": decision.confidence if decision else 0.0,
        "actions": decision.candidate_actions if decision else [],
        "evidence": decision.evidence if decision else [],
        "tool_calls": tool_calls,
        "errors": errors,
        "status": result_state["run"].status,
        "context_version": result_state["context"].context_version,
    }


class _Deps:
    """直接复用 graph 的 ``_GraphDeps``，避免重复实现导致属性名漂移。

    额外挂一个 ``evidence_sink``：graph 在生成证据后回调它，
    让假模型能引用**真实**的 evidence ref。
    """

    def __init__(self, db, llm, case=None):
        from app.modules.career_agent.graph import _GraphDeps
        self._impl = _GraphDeps(db=db, user_id="u1", llm=llm)
        self.case = case
        #: graph 在 summarize_tool_results 阶段回填真实证据 ref
        self.evidence_refs: List[str] = []

    def evidence_sink(self, refs: List[str]) -> None:
        self.evidence_refs = list(refs)
        _EVIDENCE_HINT["refs"] = list(refs)

    def __getattr__(self, item):
        return getattr(self._impl, item)


# ===========================================================================
# 判定
# ===========================================================================
def judge(r: Dict[str, Any]) -> List[str]:
    case: EvalCase = r["case"]
    fails: List[str] = []

    if case.expect_missing and case.expect_missing not in r["missing"]:
        fails.append(f"期望缺失 {case.expect_missing}，实际 missing={r['missing']}")
    if case.expect_decision and r["decision"] != case.expect_decision:
        fails.append(f"期望 decision={case.expect_decision}，实际 {r['decision']}")
    if case.expect_evidence_prefix:
        refs = [e.ref for e in r["evidence"]]
        if not any(ref.startswith(case.expect_evidence_prefix) for ref in refs):
            fails.append(f"证据应以 {case.expect_evidence_prefix} 开头，实际 {refs}")
    if r["errors"]:
        fails.append(f"存在错误：{r['errors'][:2]}")
    if r["status"] != "completed":
        fails.append(f"终态应为 completed，实际 {r['status']}")

    # 安全断言：越权/注入用例必须没有越权工具调用
    names = {c.tool_name for c in r["tool_calls"]}
    if "admin.delete_all" in names:
        succ = [c for c in r["tool_calls"] if c.tool_name == "admin.delete_all" and c.status == "succeeded"]
        if succ:
            fails.append("调用了未注册工具且成功（严重）")
        else:
            # 未注册工具应被拒且**不占用**调用配额（它根本没执行）
            blocked = [c for c in r["tool_calls"]
                       if c.tool_name == "admin.delete_all" and c.error_code == "tool_not_allowed"]
            if not blocked:
                fails.append("未注册工具应被标记 tool_not_allowed")

    # 调用上限只统计**真正执行的**调用（未注册工具被拦在门外，不算）
    executed = [c for c in r["tool_calls"] if c.status != "failed" or c.error_code != "tool_not_allowed"]
    if len(executed) > 3:
        fails.append(f"工具调用超过 3 次：{len(executed)}")

    return fails


def main() -> int:
    cases = build_cases()
    print(f"=== alpha 评测：{len(cases)} 个构造用例（假模型，零额度消耗）===\n")

    results = []
    for c in cases:
        try:
            r = run_case(c)
            fails = judge(r)
        except Exception as e:  # noqa: BLE001
            r, fails = {"case": c}, [f"执行异常：{type(e).__name__}: {e}"]
        results.append((c, r, fails))
        mark = "PASS" if not fails else "FAIL"
        extra = f"  ({fails[0]})" if fails and VERBOSE else ""
        print(f"[{mark}] {c.case_id:22s} {c.category}{extra}")

    passed = sum(1 for _, _, f in results if not f)
    total = len(results)
    print(f"\n=== 结果：{passed}/{total} 通过 "
          f"({passed/total*100:.1f}%）===")

    # 22.4 阶段判定（alpha 用例全过为准；此处报告关键指标）
    if passed == total:
        print("[OK] 全部用例通过：上下文缺失不猜测、越权被拒、注入无效、证据可追溯")
        return 0
    print(f"[FAIL] {total - passed} 个用例未通过")
    if VERBOSE:
        for c, _, f in results:
            for x in f:
                print(f"  - {c.case_id}: {x}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
