"""``ToolCatalog v1`` —— Agent 与业务模块之间的**外部工具 ABI**。

设计依据：`docs/agent/v0-agent-capability-boundary.md` 第 9/13 节。

为什么是"代码优先"而不是 YAML（第 21.3 节）：
- ``ToolSpec`` 用 Pydantic，模型工具 schema 由 ``input_model.model_json_schema()`` 生成；
- **不引入 YAML 作为第二份真相**（两份真相必然漂移）；
- 不把 ToolCatalog 存进数据库（工具定义是代码资产，不是运行时数据）。

**这个文件是工具注册的唯一来源**（13 节）：模型工具定义、后端参数校验、
前端工具轨迹、权限检查和测试都从这里读或生成。

红线（3 节 / 13.1）：
- ``user_id`` **只能由运行时注入**，模型、前端、工具参数都不能覆盖。
- 工具只允许调用领域模块的公开 capability，**不得**导入业务私有函数、
  不得执行任意 SQL / Shell / 外部 HTTP。
- 写工具必须 ``confirmation_required=True`` 且 ``idempotency_required=True``。
- 模型只能看到错误摘要，**看不到**异常栈 / SQL / 密钥 / 内部实现细节。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ===========================================================================
# 契约名 ↔ provider 线名
# ===========================================================================
#: 契约名（``domain.action``，13.1 节要求）与 provider 函数名不是同一个东西。
#: OpenAI 兼容接口对 ``tools[].function.name`` 有硬约束：
#:
#:     ^[a-zA-Z0-9_-]{1,64}$
#:
#: 即**不允许点号**。DeepSeek 会直接返回 400：
#:     Invalid 'tools[0].function.name': string does not match pattern
#: （2026-10-03 真模型验证实测，见 docs/agent/v0-2-retrospective.md 第 10 章）
#:
#: 因此工具名必须映射：``resume.get`` → ``resume_get``。
#: **映射是双向且唯一可逆的**：模型只能看到线名，网关在入口处换回契约名，
#: 于是「模型可见的名字」与「我们审计/校验用的名字」不会出现两份真相。
WIRE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
WIRE_NAME_MAX = 64


def to_wire_name(canonical: str) -> str:
    """契约名 → provider 线名（点号换下划线）。"""
    return canonical.replace(".", "_")

# ===========================================================================
# 统一状态枚举（13.2 节）—— 只允许这 10 个值
# ===========================================================================
ToolStatus = Literal[
    "succeeded",
    "empty",
    "needs_input",
    "forbidden",
    "not_found",
    "stale",
    "conflict",
    "rate_limited",
    "timeout",
    "failed",
]

TOOL_STATUSES = frozenset({
    "succeeded", "empty", "needs_input", "forbidden", "not_found",
    "stale", "conflict", "rate_limited", "timeout", "failed",
})


# ===========================================================================
# ToolSpec / ToolResult
# ===========================================================================
class RetryPolicy(BaseModel):
    max_retries: int = 0
    backoff_seconds: float = 0.0


class RateLimit(BaseModel):
    #: alpha 阶段不限流（并发由 ⑧ 的 generation_semaphore 统一控制）
    max_per_minute: Optional[int] = None


class ToolSpec(BaseModel):
    """工具的机器可读契约。每个字段都有硬约束（13.1 表）。"""

    name: str                              # 必须 domain.action
    version: int = 1
    description: str
    kind: Literal["read", "write", "internal"]
    enabled: bool = True
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    required_scopes: List[str] = Field(default_factory=list)
    side_effect: Literal["none", "write"] = "none"
    timeout_ms: int = 15000
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    confirmation_required: bool = False
    idempotency_required: bool = False
    evidence_policy: Literal["none", "optional", "required"] = "optional"
    #: 指向领域 capability 的可调用对象；**不存字符串路径**（避免任意调用）
    adapter: Optional[Callable[..., Any]] = None

    def model_schema(self) -> Dict[str, Any]:
        """给模型看的工具 schema（由 input_model 生成，不手写第二份）。"""
        return self.input_model.model_json_schema()

    @property
    def wire_name(self) -> str:
        """模型/接口侧可见的工具名（见文件头「契约名 ↔ provider 线名」）。"""
        return to_wire_name(self.name)

    @model_validator(mode="after")
    def _enforce_invariants(self) -> "ToolSpec":
        """强制 13.1 的字段约束。

        ⚠️ 这里必须用 ``@model_validator``，**不能用 ``__post_init__``**——
        Pydantic v2 的 ``BaseModel.__init__`` 不会调用 ``__post_init__``，
        写在里面的校验等于形同虚设（本项目已实测踩过：约束全部失效）。
        """
        if "." not in self.name or self.name.split(".")[0] == self.name:
            raise ValueError(f"工具名必须用 domain.action 格式：{self.name}")
        # 线名必须能被 provider 接受，否则真实调用会在 HTTP 400 上失败——
        # 这个校验是那次真模型失败的回归闸门：放一个含点号的名字进来就报错。
        wire = self.wire_name
        if not WIRE_NAME_PATTERN.match(wire) or len(wire) > WIRE_NAME_MAX:
            raise ValueError(
                f"工具线名不符合 provider 约束 ^[a-zA-Z0-9_-]{{1,{WIRE_NAME_MAX}}}$："
                f"{self.name} -> {wire}"
            )
        if self.kind == "write":
            if self.side_effect != "write":
                raise ValueError(f"写工具必须显式声明 side_effect='write'：{self.name}")
            if not self.confirmation_required:
                raise ValueError(f"写工具必须 confirmation_required=True：{self.name}")
            if not self.idempotency_required:
                raise ValueError(f"写工具必须 idempotency_required=True：{self.name}")
        if self.adapter is not None and not callable(self.adapter):
            raise ValueError(f"adapter 必须是可调用对象，不能是路径字符串：{self.name}")
        return self


class ToolResult(BaseModel):
    """统一信封。所有工具必须返回它，禁止直接返回 ORM / 异常 / 任意 JSON。"""

    status: ToolStatus
    tool_name: str
    tool_version: int = 1
    data: Dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    object_refs: List[Dict[str, Any]] = Field(default_factory=list)
    object_version: Optional[str] = None
    evidence_ref: Optional[str] = None
    error_code: Optional[str] = None
    retryable: bool = False
    latency_ms: int = 0

    @model_validator(mode="after")
    def _check_status(self) -> "ToolResult":
        """状态只允许 13.2 定义的 10 个值。

        ⚠️ 同 ToolSpec：必须用 ``@model_validator``，Pydantic v2 不调用 ``__post_init__``。
        """
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"非法工具状态：{self.status}")
        return self


# ===========================================================================
# 首批工具的输入输出模型（Pydantic，禁止裸 dict —— 13.1）
# ===========================================================================
class ResumeGetInput(BaseModel):
    resume_id: str


class JdGetInput(BaseModel):
    jd_id: str


class MatchGetInput(BaseModel):
    resume_id: str
    jd_id: str


class ApplicationGetInput(BaseModel):
    application_id: str


class ApplicationUpdateStatusInput(BaseModel):
    """写工具输入（设计文档 5.2 的 ``application.update_status``）。

    ⚠️ **没有 ``expected_status`` 之外的版本字段，也没有 ``user_id``**：
    - ``user_id`` 由网关在运行时注入（模型无从指定身份，3 节）；
    - ``expected_status`` 由服务端在 ``prepare_write_action`` 里**从上下文取**
      （不看模型），模型只能提议目标状态。
      让模型自己填期望值等于让它自己决定"我基于哪个版本写"，CAS 会失去意义。
    """

    application_id: str
    expected_status: str
    new_status: str
    reason: str = ""


# ===========================================================================
# 工具注册表
# ===========================================================================
def _build_catalog() -> Dict[str, ToolSpec]:
    """组装首批只读工具。

    adapter 全部指向 **capability 层**（``*_capability`` 函数），
    不指向 services 私有函数——这是"Agent 不重新实现业务逻辑"的强制边界。
    """
    from app.modules.application.capabilities import (
        get_application_capability,
        update_status_capability,
    )
    from app.modules.jd.capabilities import get_jd_capability
    from app.modules.match.capabilities import get_match_capability
    from app.modules.resume.capabilities import get_resume_capability

    specs = [
        ToolSpec(
            name="resume.get",
            version=1,
            description="读取一份简历的元数据、结构化摘要与版本。不返回完整正文。",
            kind="read",
            input_model=ResumeGetInput,
            output_model=ToolResult,
            required_scopes=["resume:read"],
            side_effect="none",
            evidence_policy="required",
            adapter=get_resume_capability,
        ),
        ToolSpec(
            name="jd.get",
            version=1,
            description="读取一个 JD 的结构化要求与引用。不返回完整 JD 正文。",
            kind="read",
            input_model=JdGetInput,
            output_model=ToolResult,
            required_scopes=["jd:read"],
            side_effect="none",
            evidence_policy="required",
            adapter=get_jd_capability,
        ),
        ToolSpec(
            name="match.get",
            version=1,
            description="读取一份简历与一个 JD 的已有匹配结果（分/亮点/缺口）。不重算。",
            kind="read",
            input_model=MatchGetInput,
            output_model=ToolResult,
            required_scopes=["match:read"],
            side_effect="none",
            evidence_policy="required",
            adapter=get_match_capability,
        ),
        ToolSpec(
            name="application.get",
            version=1,
            description="读取一条投递记录的当前状态与可变更状态列表。不返回备注正文。",
            kind="read",
            input_model=ApplicationGetInput,
            output_model=ToolResult,
            required_scopes=["application:read"],
            side_effect="none",
            evidence_policy="required",
            adapter=get_application_capability,
        ),
        # ---- v0.2-beta 首个写入工具（设计文档 5.2）-------------------------
        # ``kind="write"`` 让 ``ToolSpec`` 的校验器强制要求
        # side_effect="write" + confirmation_required + idempotency_required，
        # 同时也让 ``_record_calls`` 在工具选择阶段直接拒绝它——
        # 写操作**只能**经 ActionIntent + interrupt 用户确认（9.2 节）。
        ToolSpec(
            name="application.update_status",
            version=1,
            description=(
                "把一条投递记录的状态推进到下一个状态。"
                "必须经用户确认后才会真正执行；执行前会校验状态未被他人改动。"
            ),
            kind="write",
            input_model=ApplicationUpdateStatusInput,
            output_model=ToolResult,
            required_scopes=["application:write"],
            side_effect="write",
            evidence_policy="required",
            confirmation_required=True,
            idempotency_required=True,
            adapter=update_status_capability,
        ),
    ]
    return {spec.name: spec for spec in specs}


CATALOG: Dict[str, ToolSpec] = _build_catalog()


def _build_wire_map(catalog: Dict[str, ToolSpec]) -> Dict[str, str]:
    """线名 → 契约名。

    构造期就校验唯一性：两个工具映射到同一个线名会让模型的调用**语义歧义**
    （调 ``a_b`` 到底是 ``a.b`` 还是 ``a_b``），必须在启动时炸掉，而不是线上随机选一个。
    同时把契约名也映射到自身，兼容「模型直接回吐契约名」的情形。
    """
    mapping: Dict[str, str] = {}
    for name, spec in catalog.items():
        wire = spec.wire_name
        owner = mapping.get(wire)
        if owner is not None and owner != name:
            raise RuntimeError(f"工具线名冲突：{wire} 同时属于 {owner} 与 {name}")
        mapping[wire] = name
        mapping[name] = name
    return mapping


#: 线名/契约名 → 规范的契约名。网关与 Graph 用它在入口处归一。
WIRE_TO_CANONICAL: Dict[str, str] = _build_wire_map(CATALOG)


def resolve_tool_name(name: str) -> str:
    """把模型给的工具名归一成契约名。

    **未注册的名字原样返回**——不要试图猜一个近似的已注册工具，
    否则模型（或注入内容）可以用一个错名字摸到别的工具。原样返回后，
    调用方按「未注册」处理并留 ``tool_not_allowed`` 审计记录。
    """
    canonical = WIRE_TO_CANONICAL.get(name)
    if canonical == name:
        return name
    if canonical is None:
        # ``name`` 可能是把自己点号写歪的契约名（如 ``abc.def``），
        # 这种情况也不做模糊匹配，一律原样返回。
        return name
    return canonical


def get_tool(name: str) -> ToolSpec:
    """按名取工具；未注册或未启用都算不可用。"""
    spec = CATALOG.get(name)
    if spec is None:
        raise KeyError(f"工具未注册：{name}")
    if not spec.enabled:
        raise RuntimeError(f"工具已禁用：{name}")
    return spec


def list_tools(include_disabled: bool = False) -> List[ToolSpec]:
    if include_disabled:
        return list(CATALOG.values())
    return [s for s in CATALOG.values() if s.enabled]


# ===========================================================================
# ToolGateway —— 唯一执行入口（9.2 节）
# ===========================================================================
class ToolGateway:
    """工具执行网关。

    职责（9.2）：身份/权限/参数/超时检查 → 执行 → 包 ``ToolResult`` → 记审计。
    **模型永远拿不到异常栈**。

    名字权威（与文件头「契约名 ↔ provider 线名」配套）：
    ``invoke()`` 入口调用 ``resolve_tool_name()``，因此**线名与契约名都能直接调用**；
    返回的 ``ToolResult.tool_name`` **恒为契约名**，下游（证据 ref、审计、Trace）
    只认契约名。
    """

    def __init__(self, db, user_id: str) -> None:
        # user_id 由运行时注入并绑定在此，不作为任何工具参数传递
        self._db = db
        self._user_id = user_id

    def build_model_tools(self, *, kind: str = "read") -> List[Dict[str, Any]]:
        """给 LLM 的工具声明（OpenAI function-calling 格式）。

        **只暴露只读工具**（``kind="read"``）。写工具**绝不能**出现在模型的
        function-calling 清单里——一旦出现，模型就能"直接调用"它，
        而设计文档 9.2 要求写操作必须先生成 ``ActionIntent`` 并等用户确认。
        写工具是经 ``build_model_input`` 的上下文 + compose 轮的 action 提议
        进入流程的，不是经 ``tools=[...]``。

        另外两点硬约束：
        - **不含 ``user_id``/``db``**——模型无从指定调用身份；
        - ``function.name`` 用**线名**（``resume_get``）而不是契约名
          （``resume.get``）。含点号会被 DeepSeek/OpenAI 直接 400 拒绝，
          见文件头「契约名 ↔ provider 线名」。模型回吐的线名由
          ``resolve_tool_name`` 换回契约名。
        """
        out: List[Dict[str, Any]] = []
        for spec in list_tools():
            if spec.kind != kind:
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": spec.wire_name,
                    "description": spec.description,
                    "parameters": spec.model_schema(),
                },
            })
        return out

    def invoke(self, tool_name: str, arguments: Dict[str, Any]) -> ToolResult:
        """执行一个工具。任何失败都返回结构化 ``ToolResult``，不抛异常给模型。

        **入口先做线名归一**：调用方给的可能不是契约名。两种形态都必须能用——

        - 契约名 ``resume.get``：内部调用（Graph 的选工具结果、测试、脚本）；
        - provider 线名 ``resume_get``：模型回吐的名字，或任何照抄模型输出的调用方。

        归一放在**网关入口**而不是只放在上游，理由是「网关是唯一执行入口」：
        只要有一个上游漏了归一（新增节点、脚本、未来的写工具确认路径），
        整条调用就会因为一个点号而失败。放在这里等于把「名字权威」收敛到一处，
        上层归一变成"提前量"而非"正确性前提"。

        **不做模糊匹配**：``resolve_tool_name`` 对未注册的名字原样返回，
        因而仍然按「未注册」处理并留 ``unknown_tool``——不能用一个错名字摸到别的工具。
        """
        call_id = str(uuid.uuid4())
        started = time.monotonic()

        canonical = resolve_tool_name(tool_name)

        try:
            spec = get_tool(canonical)
        except KeyError as e:
            return self._error(canonical, "unknown_tool", str(e), call_id, started)
        except RuntimeError as e:
            return self._error(canonical, "tool_disabled", str(e), call_id, started)

        # 参数校验：Pydantic，失败返回结构化错误而非 500
        try:
            validated = spec.input_model(**(arguments or {}))
        except Exception as e:  # noqa: BLE001
            return self._error(
                canonical, "invalid_arguments", f"参数校验失败：{e}", call_id, started,
            )

        kwargs = validated.model_dump()

        # 身份注入：resume.get/jd.get/match.get 需要 db + user_id，
        # 这两个参数**不来自模型**，由网关补齐。
        kwargs["db"] = self._db
        kwargs["user_id"] = self._user_id

        try:
            raw = spec.adapter(**kwargs)
        except Exception as e:  # noqa: BLE001
            # 只回摘要，绝不回异常栈/SQL。日志与信封统一用**契约名**，
            # 便于审计与证据 ref 对齐（线名只在模型侧存在）。
            logger.warning("工具 %s 执行失败：%s", canonical, type(e).__name__)
            return self._error(
                canonical, "tool_exception", f"工具执行异常：{type(e).__name__}",
                call_id, started, retryable=False,
            )

        return self._to_result(spec, raw, call_id, started)

    # ------------------------------------------------------------------
    @staticmethod
    def _to_result(spec: ToolSpec, raw: Any, call_id: str, started: float) -> ToolResult:
        """把 capability 的返回 dict 包装成统一 ``ToolResult``。"""
        latency = int((time.monotonic() - started) * 1000)

        if not isinstance(raw, dict):
            return ToolResult(
                status="failed", tool_name=spec.name, tool_version=spec.version,
                error_code="bad_tool_return", retryable=False, latency_ms=latency,
                summary="工具返回了非法结构",
            )

        status = raw.get("status", "succeeded")

        # capability 返回的状态是**业务事实**，必须原样保留：
        # - ``not_found``：对象不存在或不属于该用户（信息泄露防护在 capability 层完成）；
        # - ``conflict``：CAS 失败（写工具发现状态已被他人改动）——**绝不能降级成
        #   succeeded**，否则「基于过期上下文写入」会静默变成「写成功了」；
        # - 其余 ``empty`` / ``needs_input`` / ``forbidden`` / ``stale`` /
        #   ``rate_limited`` / ``timeout`` / ``failed`` 同理。
        #
        # 早期实现只特判了 ``not_found``，其余一律按 succeeded 返回。那是一个真实的
        # 缺陷类别：任何 capability 想表达"没成功"都会被网关**改写成功**。
        if status in TOOL_STATUSES and status != "succeeded":
            payload = {k: v for k, v in raw.items() if k not in ("status", "message")}
            refs = payload.pop("object_refs", []) or []
            version = payload.pop("object_version", None)
            return ToolResult(
                status=status, tool_name=spec.name, tool_version=spec.version,
                data=payload,
                summary=raw.get("message") or _summarize(spec.name, raw),
                object_refs=refs,
                object_version=version,
                error_code=raw.get("reason") or status,
                retryable=False, latency_ms=latency,
            )

        payload = {k: v for k, v in raw.items() if k not in ("status", "message")}
        refs = payload.pop("object_refs", []) or []
        version = payload.pop("object_version", None)

        return ToolResult(
            status="succeeded", tool_name=spec.name, tool_version=spec.version,
            data=payload,
            summary=_summarize(spec.name, payload),
            object_refs=refs,
            object_version=version,
            evidence_ref=f"{spec.name}:{call_id}",
            latency_ms=latency,
        )

    @staticmethod
    def _error(
        tool_name: str, code: str, message: str, call_id: str,
        started: float, retryable: bool = False,
    ) -> ToolResult:
        return ToolResult(
            status="failed", tool_name=tool_name,
            error_code=code, summary=message, retryable=retryable,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _summarize(tool_name: str, payload: Dict[str, Any]) -> str:
    """给用户看的短摘要（不泄露内部细节）。"""
    if tool_name == "match.get" and "match" in payload:
        m = payload["match"]
        return f"匹配分 {m.get('score')}，亮点 {len(m.get('highlights') or [])} 项，硬缺口 {len(m.get('hard_gaps') or [])} 项"
    if tool_name == "resume.get" and "resume" in payload:
        r = payload["resume"]
        return f"简历版本 v{r.get('version')}，字数约 {r.get('word_count')}"
    if tool_name == "jd.get" and "jd" in payload:
        j = payload["jd"]
        req = j.get("requirements") or {}
        return f"{j.get('company')} / {j.get('position')}，必备 {len(req.get('must_have') or [])} 项"
    if tool_name == "application.update_status" and "application" in payload:
        a = payload["application"]
        return (
            f"{a.get('company')} / {a.get('position')}："
            f"{payload.get('previous_status')} → {a.get('status')}"
        )
    if tool_name == "application.get" and "application" in payload:
        a = payload["application"]
        return f"{a.get('company')} / {a.get('position')}，当前状态 {a.get('status')}"
    return "已返回结果"
