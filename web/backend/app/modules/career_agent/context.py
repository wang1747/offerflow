"""``ContextBuilder`` —— 组装结构化 ``AgentContext``。

设计依据：设计文档第 7 节（AgentContext 契约）与 21.4 节。

**这个类不调用 LLM、不生成行动、不执行写操作**，只做确定性组装：
1. 校验身份与对象权限
2. 解析 ``context_refs``
3. 通过**只读 capability** 读取结果
4. 固定对象版本并计算 ``context_hash``
5. 检查缺失上下文与版本冲突
6. 按预算裁剪摘要

核心红线：**引用不存在时不猜用户意图，返回 missing_context → needs_input**。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from app.modules.career_agent.state import (
    ContextObject,
    ContextRef,
    ContextState,
    MissingContext,
)

logger = logging.getLogger(__name__)

#: alpha 默认 token 预算（设计文档第 7 节 AgentContext.budget）
DEFAULT_MAX_CONTEXT_TOKENS = 6000


class ContextWarning(BaseModel):
    code: str
    message: str


class ContextBuildResult(BaseModel):
    """组装结果。``ok=False`` 时 Agent 必须返回 needs_input。"""

    context: ContextState
    missing_context: List[MissingContext] = Field(default_factory=list)
    warnings: List[ContextWarning] = Field(default_factory=list)
    context_hash: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.missing_context


class ContextBuilder:
    """按只读 capability 组装上下文。"""

    def __init__(self, db, user_id: str) -> None:
        # 身份绑定在此，不来自模型
        self._db = db
        self._user_id = user_id

    def build(
        self,
        context_refs: Optional[List[ContextRef]] = None,
        entry_source: str = "unknown",
        token_budget: int = DEFAULT_MAX_CONTEXT_TOKENS,
    ) -> ContextBuildResult:
        from app.modules.application.capabilities import get_application_capability
        from app.modules.jd.capabilities import get_jd_capability
        from app.modules.resume.capabilities import (
            get_resume_capability,
            list_resumes_capability,
        )

        refs = list(context_refs or [])
        warnings: List[ContextWarning] = []
        missing: List[MissingContext] = []
        objects: List[ContextObject] = []
        versions: List[str] = []

        jd_ref = next((r for r in refs if r.type == "jd"), None)
        resume_ref = next((r for r in refs if r.type == "resume"), None)
        application_ref = next((r for r in refs if r.type == "application"), None)

        # ---- 1) JD --------------------------------------------------------
        if jd_ref:
            res = get_jd_capability(self._db, self._user_id, jd_ref.id)
            if res.get("status") == "not_found":
                # 不区分"不存在"与"无权限"——对 Agent 统一为缺失
                missing.append(MissingContext(
                    type="jd", reason="not_found",
                    hint="目标 JD 不存在或不属于当前用户，请重新选择。",
                ))
            else:
                jd = res["jd"]
                versions.append(res.get("object_version") or f"jd:{jd_ref.id}:noversion")
                objects.append(ContextObject(
                    type="jd", id=jd["id"], version=None,
                    company=jd.get("company"), position=jd.get("position"),
                    summary={
                        "requirements": jd.get("requirements", {}),
                        "preview": jd.get("preview", ""),
                        "has_parsed": bool(jd.get("requirements", {}).get("must_have")
                                           or jd.get("requirements", {}).get("tech_stack")),
                    },
                ))
                if not jd.get("has_embedding"):
                    warnings.append(ContextWarning(
                        code="jd_no_embedding",
                        message="该 JD 尚无语义向量，匹配分析可能不可用。",
                    ))
        else:
            missing.append(MissingContext(
                type="jd", reason="absent",
                hint="请先选择一个目标 JD，Agent 不会自行猜测。",
            ))

        # ---- 2) 简历 ------------------------------------------------------
        if resume_ref:
            res = get_resume_capability(self._db, self._user_id, resume_ref.id)
            if res.get("status") == "not_found":
                missing.append(MissingContext(
                    type="resume", reason="not_found",
                    hint="简历不存在或不属于当前用户，请重新选择。",
                ))
            else:
                r = res["resume"]
                versions.append(res.get("object_version") or f"resume:{r['id']}:v{r.get('version')}")
                objects.append(ContextObject(
                    type="resume", id=r["id"], version=r.get("version"),
                    summary={
                        "name": r.get("summary", {}).get("name"),
                        "skills": r.get("summary", {}).get("skills", []),
                        "word_count": r.get("word_count"),
                        "has_structured": r.get("summary", {}).get("has_structured"),
                    },
                ))
        else:
            # 多简历不猜：提示用户选择（设计文档 2.4 简历选择规则）
            listed = list_resumes_capability(self._db, self._user_id, limit=5)
            if listed["total"] == 0:
                missing.append(MissingContext(
                    type="resume", reason="absent",
                    hint="当前没有简历，请先上传或粘贴一份简历。",
                ))
            elif listed["total"] == 1:
                # 只有一份：自动选中（设计文档允许）
                res = get_resume_capability(self._db, self._user_id, listed["items"][0]["id"])
                if res.get("status") == "succeeded":
                    r = res["resume"]
                    versions.append(res.get("object_version") or "")
                    objects.append(ContextObject(
                        type="resume", id=r["id"], version=r.get("version"),
                        summary={
                            "name": r.get("summary", {}).get("name"),
                            "skills": r.get("summary", {}).get("skills", []),
                        },
                    ))
                    warnings.append(ContextWarning(
                        code="resume_autoselected",
                        message="只有一个简历，已自动选中。",
                    ))
            else:
                missing.append(MissingContext(
                    type="resume", reason="ambiguous",
                    hint=f"检测到 {listed['total']} 份简历，请明确选择一份（不自动猜测）。",
                ))

        # ---- 3) 投递记录（**可选**上下文）----------------------------------
        # 与 jd/resume 的关键差别：不传 application 时**不算缺失**——
        # 只读规划完全不需要它。但一旦用户显式传了 id 却查不到，就按缺失处理：
        # 那是"用户以为存在的对象没了"，静默忽略会让 Agent 基于错误前提规划。
        if application_ref:
            res = get_application_capability(self._db, self._user_id, application_ref.id)
            if res.get("status") == "not_found":
                missing.append(MissingContext(
                    type="application", reason="not_found",
                    hint="投递记录不存在或不属于当前用户，请重新选择。",
                ))
            else:
                a = res["application"]
                versions.append(
                    res.get("object_version") or f"application:{a['id']}:{a.get('status')}"
                )
                objects.append(ContextObject(
                    type="application", id=a["id"], version=None,
                    company=a.get("company"), position=a.get("position"),
                    summary={
                        "status": a.get("status"),
                        "next_action": a.get("next_action"),
                        # 可写状态集合来自 capability，不是硬编码——写入规则
                        # 的真相在领域模块里，ContextBuilder 只搬运。
                        "available_statuses": res.get("available_statuses", []),
                        "agent_writable_statuses": res.get("agent_writable_statuses", []),
                    },
                ))

        # ---- 4) 固定版本 + context_hash ----------------------------------
        context_version = "|".join(sorted(v for v in versions if v))
        context_hash = hashlib.sha256(
            json.dumps(
                {
                    "user": self._user_id,
                    "objects": [
                        {"type": o.type, "id": o.id, "version": o.version}
                        for o in objects
                    ],
                    "version": context_version,
                },
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]

        # ---- 5) 按预算裁剪 ------------------------------------------------
        objects = self._fit_budget(objects, token_budget, warnings)

        return ContextBuildResult(
            context=ContextState(
                active_objects=objects,
                context_version=context_version or None,
                missing_context=missing,
            ),
            missing_context=missing,
            warnings=warnings,
            context_hash=context_hash,
        )

    @staticmethod
    def _fit_budget(
        objects: List[ContextObject],
        token_budget: int,
        warnings: List[ContextWarning],
    ) -> List[ContextObject]:
        """粗略按字符裁剪（中文约 1 字 ≈ 1 token）。

        先砍 JD 的 preview，再砍 skills——保留 must_have 与 name。
        """
        # 每字符约 1 token 的粗略估算，预留 30% 给消息与工具结果
        limit = max(500, int(token_budget * 0.7))

        budget = limit
        trimmed: List[ContextObject] = []
        for obj in objects:
            summary = dict(obj.summary)
            # 优先丢 preview（可从 capability 重新读取）
            if "preview" in summary:
                cost = len(str(summary["preview"]))
                if budget - cost < 0:
                    summary.pop("preview")
                    warnings.append(ContextWarning(
                        code="preview_trimmed",
                        message=f"{obj.type} 摘要超预算，已裁剪预览。",
                    ))
                else:
                    budget -= cost
            if "skills" in summary and isinstance(summary["skills"], list):
                kept = summary["skills"][:20]
                if len(kept) < len(summary["skills"]):
                    summary["skills"] = kept
                    warnings.append(ContextWarning(
                        code="skills_trimmed",
                        message=f"{obj.type} 技能列表已裁剪到 20 项。",
                    ))
            trimmed.append(ContextObject(
                type=obj.type, id=obj.id, version=obj.version,
                company=obj.company, position=obj.position, summary=summary,
            ))
        return trimmed
