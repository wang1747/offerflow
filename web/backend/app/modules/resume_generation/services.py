"""简历生成服务：结构化生成、逐段重生成、评分与 JD 匹配。"""

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.agents.tools.ats_checker import check_ats_compatibility
from app.core.concurrency import ainvoke_llm, run_sync
from app.core.llm import get_user_llm_or_raise
from app.models.jd import JobDescription
from app.models.user import User
from app.modules.resume.optimizer.contracts import JDRequirement
from app.modules.resume.optimizer.gap import analyze_gap
from app.modules.resume.optimizer.service import (
    _ONE_PAGE_MAX_CHARS,
    _condense_to_one_page,
    _estimate_too_long,
)
from app.observability.tracing import trace_operation

from .contracts import GenerationResult
from .fidelity import (
    canonical_numbers,
    check_bullet_fidelity,
    sanitize_structured,
    sanitize_text,
)
from .page_fit import is_too_long as _one_page_exceeded
from .page_fit import restructure_from_text
from .quality import report_to_dict, score_resume_quality
from .semantic import score_semantic_quality
from .sufficiency import assess_input_sufficiency
from .prompts import (
    REGENERATE_SYSTEM_PROMPT,
    REGENERATE_VARIANTS_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_regenerate_user_prompt,
    build_user_prompt,
)
from .render import list_sections, render_resume_text
from .schemas import ResumeGenerateRequest

logger = logging.getLogger(__name__)


def _clean_json_response(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def _dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _is_blank(value: Any) -> bool:
    """None / 空串 / 空列表 / 空字典 都算"模型没给这个字段"。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


# 逐段重写只允许改表达层；这些字段是事实层，永远不能由模型覆盖。
_IMMUTABLE_FACT_KEYS = ("name", "position", "contact", "education", "skills", "certifications")


def _merge_regenerate_payload(
    payload: Any,
    structured: Dict[str, Any],
) -> Dict[str, Any]:
    """把「逐段重写」的模型返回补全成一份完整 structured。

    为什么需要：逐段重写要求模型回吐**整份** JSON（只改指定字段），
    但模型经常只回被改的那一段。生产实测：一次 `resume_regenerate_section`
    的 completion 只有 559 tokens 就结束了，name 缺失 → `_build_result` 直接
    返回"生成结果缺少姓名，请重试"，用户点了"重写这一段"却整份失败。
    （离线单测抓不到：conftest 的假模型对这条提示词是 `dict(current)` 完美回吐。）

    规则 —— 事实字段以用户手上的 structured 为唯一真源：
      * name/position/contact/education/skills/certifications 永远回填来源；
      * experiences 只接受模型重写的 bullets，项目名/公司/角色/时间按原顺序回填；
      * 模型少回经历时保留来源中的完整经历，多回/改名时丢弃额外项；
      * 只有 summary 等表达层字段允许模型覆盖。

    这样即使模型返回幻觉项目、学校或角色，也不可能通过合并进入最终版本。
    """
    source = structured or {}
    merged: Dict[str, Any] = {}
    if isinstance(payload, dict):
        merged.update({k: v for k, v in payload.items() if not _is_blank(v)})

    for key in _IMMUTABLE_FACT_KEYS:
        value = source.get(key)
        if _is_blank(value):
            merged.pop(key, None)
        else:
            merged[key] = copy.deepcopy(value)

    source_experiences = source.get("experiences")
    if isinstance(source_experiences, list):
        model_experiences = payload.get("experiences") if isinstance(payload, dict) else None
        merged_experiences: List[Dict[str, Any]] = []
        for index, source_item in enumerate(source_experiences):
            if not isinstance(source_item, dict):
                continue
            item = copy.deepcopy(source_item)
            model_item = (
                model_experiences[index]
                if isinstance(model_experiences, list) and index < len(model_experiences)
                else None
            )
            if isinstance(model_item, dict) and isinstance(model_item.get("bullets"), list):
                item["bullets"] = [
                    str(bullet).strip()
                    for bullet in model_item["bullets"]
                    if isinstance(bullet, str) and bullet.strip()
                ]
            merged_experiences.append(item)
        merged["experiences"] = merged_experiences
    elif _is_blank(source_experiences):
        merged.pop("experiences", None)

    for key, value in source.items():
        if key not in merged and not _is_blank(value):
            merged[key] = copy.deepcopy(value)

    return merged


async def _build_jd_requirement(
    jd: Optional[JobDescription],
    jd_text: str,
    user: User,
) -> JDRequirement:
    if jd is not None:
        tech_stack: List[str] = []
        ts = jd.tech_stack or {}
        if isinstance(ts, dict):
            for values in ts.values():
                if isinstance(values, list):
                    tech_stack.extend(str(item) for item in values)
        elif isinstance(ts, list):
            tech_stack = [str(item) for item in ts]
        return JDRequirement(
            raw_text=jd.raw_text or jd_text or "",
            company=jd.company or "",
            position=jd.position or "",
            must_have=jd.must_have or [],
            nice_to_have=jd.nice_to_have or [],
            tech_stack=tech_stack,
            hidden_signals=jd.hidden_signals or [],
        )

    # 没有结构化 JD，但有 jd_text：解析 JD 提取硬性/加分要求，让 gap 分析有据可依
    raw = (jd_text or "").strip()
    if not raw:
        return JDRequirement(raw_text="")
    try:
        from app.agents.graphs.jd_analysis import analyze_jd

        result = await analyze_jd(raw, user)
        parsed = result.get("parsed") or {}
        if parsed.get("must_have") or parsed.get("nice_to_have") or parsed.get("tech_stack"):
            tech_stack: List[str] = []
            ts = parsed.get("tech_stack") or {}
            if isinstance(ts, dict):
                for values in ts.values():
                    if isinstance(values, list):
                        tech_stack.extend(str(item) for item in values)
            elif isinstance(ts, list):
                tech_stack = [str(item) for item in ts]
            return JDRequirement(
                raw_text=raw,
                company=parsed.get("company") or "",
                position=parsed.get("position") or "",
                must_have=parsed.get("must_have") or [],
                nice_to_have=parsed.get("nice_to_have") or [],
                tech_stack=tech_stack,
                hidden_signals=parsed.get("hidden_signals") or [],
            )
    except Exception as e:
        logger.warning("简历生成时解析 JD 提取要求失败（gap 分析降级）: %s", e)
    return JDRequirement(raw_text=raw)


def _score(requirement: JDRequirement, resume_text: str) -> tuple:
    ats = check_ats_compatibility(resume_text, requirement.raw_text)
    gap = analyze_gap(resume_text, requirement)
    gap_dict = {
        "matched": gap.matched,
        "missing": gap.missing,
        "partial": gap.partial,
    }
    return ats, gap_dict


async def _build_result(
    payload: Any,
    fallback_position: str,
    jd: Optional[JobDescription],
    jd_text: str,
    user: User,
    edu_input: Optional[List[Dict[str, Any]]] = None,
    exp_input: Optional[List[Dict[str, str]]] = None,
    identity_input: Optional[Dict[str, str]] = None,
) -> GenerationResult:
    if not isinstance(payload, dict):
        return GenerationResult(error="生成结果格式错误，请重试")

    name = (payload.get("name") or "").strip()
    position = (payload.get("position") or "").strip() or (fallback_position or "").strip()
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    contact = {
        key: str(contact.get(key) or "").strip()
        for key in ("phone", "email", "github")
    }
    # 姓名与联系方式是**硬事实**，与教育经历（下）、经历事实字段（再下）同一口径：
    # 以用户填写的输入为准，不交给生成模型自由发挥。
    # 只信 LLM 输出的两个真实后果：模型漏写手机号 → 简历没有联系方式（HR 联系不到）；
    # 写错 → 错误联系方式；而接口仍返回 success=True，用户无从察觉。
    if identity_input:
        name = (identity_input.get("name") or "").strip() or name
        for key in ("phone", "email", "github"):
            value = (identity_input.get(key) or "").strip()
            if value:
                contact[key] = value
    summary = (payload.get("summary") or "").strip()
    # 教育经历是纯事实：优先用用户输入原样回填，杜绝 LLM 编造学校/学历
    education = edu_input if edu_input is not None else _dict_list(payload.get("education"))
    experiences = _dict_list(payload.get("experiences"))
    # 经历的事实字段（公司/项目名、角色、时间）也用用户输入回填，只保留 LLM 优化的 bullets
    if exp_input is not None and len(exp_input) != len(experiences):
        logger.warning(
            "生成结果经历条数与输入不一致: input=%s output=%s",
            len(exp_input),
            len(experiences),
        )
        return GenerationResult(error="生成结果丢失了部分项目或实习经历，请重试")
    if exp_input is not None:
        for i, exp in enumerate(experiences):
            src = exp_input[i]
            exp["name"] = src.get("name", "")
            exp["role"] = src.get("role", "")
            exp["start"] = src.get("start", "")
            exp["end"] = src.get("end", "")
            exp["type"] = src.get("type") or exp.get("type") or "project"
    skills = _str_list(payload.get("skills"))
    certifications = _str_list(payload.get("certifications"))
    tips = _str_list(payload.get("tips"))
    risks = _dict_list(payload.get("risks"))
    jd_alignment = _dict_list(payload.get("jd_alignment"))

    if not name:
        return GenerationResult(error="生成结果缺少姓名，请重试")
    if not education and not experiences:
        return GenerationResult(error="生成结果缺少教育经历或项目/实习经历，请重试")

    structured: Dict[str, Any] = {
        "name": name,
        "position": position,
        "contact": contact,
        "summary": summary,
        "education": education,
        "experiences": experiences,
        "skills": skills,
        "certifications": certifications,
    }
    resume_text = render_resume_text(structured)
    if not resume_text:
        return GenerationResult(error="生成结果渲染为空，请重试")

    # 一页约束第一步：超长就用「三刀裁剪法」精简。
    # **关键**：精简只作用于文本，必须把精简结果**重建回 structured** 再重新渲染，
    # 否则 structured 仍是精简前的旧数据 → 界面显示一页、导出却是两页。
    if _one_page_exceeded(resume_text):
        condensed, removed = await _try_condense(resume_text, user)
        if condensed:
            rebuilt = restructure_from_text(structured, condensed)
            rebuilt_text = render_resume_text(rebuilt)
            # 只有**确实变短**才采纳：LLM 没能精简（原样返回）时不要谎称"已精简到一页"
            if rebuilt_text and len(rebuilt_text) < len(resume_text):
                structured = rebuilt
                resume_text = rebuilt_text
                tips.append("内容较多，已自动精简到一页")
                if removed:
                    tips.append("精简内容：" + "、".join(removed))

    sections = list_sections(structured)

    requirement = await _build_jd_requirement(jd, jd_text, user)
    ats, gap = _score(requirement, resume_text)

    return GenerationResult(
        name=structured.get("name") or "",
        position=structured.get("position") or "",
        contact=structured.get("contact") or {},
        summary=structured.get("summary") or "",
        education=structured.get("education") or [],
        experiences=structured.get("experiences") or [],
        skills=structured.get("skills") or [],
        certifications=structured.get("certifications") or [],
        tips=tips,
        risks=risks,
        jd_alignment=jd_alignment,
        resume_text=resume_text,
        sections=sections,
        ats=ats,
        gap=gap,
    )


async def _try_condense(text: str, user: User) -> tuple:
    """调用「三刀裁剪法」精简；失败一律返回空，由调用方保持原样（绝不半途改坏）。"""
    try:
        with trace_operation("resume_condense_one_page", getattr(user, "id", None)):
            return await _condense_to_one_page(text, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("简历精简失败: %s", exc)
        return "", []


def _normalize_fact(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _req_input_text(req: ResumeGenerateRequest) -> str:
    """把用户填写的所有信息拼成一段文本，作为「事实来源」基准（不含 JD）。"""
    parts = [req.name, req.position, req.summary or ""]
    parts.extend([req.phone or "", req.email or "", req.github or ""])
    for edu in req.education:
        parts.extend([
            edu.school, edu.major, edu.degree, edu.start, edu.end,
            getattr(edu, "courses", "") or "", getattr(edu, "gpa", "") or "",
            getattr(edu, "honors", "") or "",
        ])
    for exp in req.experiences:
        parts.extend([exp.name, exp.role, exp.start, exp.end])
        parts.extend(exp.bullets or [])
    parts.extend(req.skills or [])
    parts.extend(req.certifications or [])
    return "\n".join(p for p in parts if p)


def _edu_input_to_dict(edu) -> Dict[str, str]:
    """把用户填写的教育经历（EducationInput）转成渲染/存库用的标准 dict。

    教育经历是纯事实（学校/专业/学历/时间），没有任何「表达优化」空间，
    必须原样保留用户输入，绝不交给 LLM 改写或编造。
    """
    detail_parts = []
    if getattr(edu, "courses", "") and str(edu.courses).strip():
        detail_parts.append(f"主修课程：{edu.courses}")
    if getattr(edu, "gpa", "") and str(edu.gpa).strip():
        detail_parts.append(f"绩点：{edu.gpa}")
    if getattr(edu, "honors", "") and str(edu.honors).strip():
        detail_parts.append(f"荣誉：{edu.honors}")
    if getattr(edu, "detail", "") and str(edu.detail).strip():
        detail_parts.append(str(edu.detail).strip())
    return {
        "school": (edu.school or "").strip(),
        "major": (edu.major or "").strip(),
        "degree": (edu.degree or "").strip(),
        "start": (edu.start or "").strip(),
        "end": (edu.end or "").strip(),
        "detail": "；".join(detail_parts),
    }


def _exp_input_to_dict(exp) -> Dict[str, str]:
    """把用户填写的经历（ExperienceInput）的事实字段抽出来。

    经历里「name/role/start/end/type」是纯事实（公司/项目名、角色、时间），
    只有 bullets 是需要 LLM 优化的表达。事实字段必须原样保留用户输入。
    """
    return {
        "type": (exp.type or "project").strip(),
        "name": (exp.name or "").strip(),
        "role": (exp.role or "").strip(),
        "start": (exp.start or "").strip(),
        "end": (exp.end or "").strip(),
    }


def check_generation_fidelity(
    req: ResumeGenerateRequest,
    result: "GenerationResult",
) -> Dict[str, Any]:
    """校验生成结果是否编造事实：学校/学历/公司名/技能/证书/联系方式必须来自用户输入。

    返回 {passed, added_schools, added_companies, added_skills, added_certifications, added_contacts}。
    不在用户输入里出现过的学校/公司名/技能/证书，视为「疑似新增」（编造）。
    """
    input_text = _req_input_text(req)
    input_norm = _normalize_fact(input_text)
    input_skills = {_normalize_fact(s) for s in (req.skills or []) if s}
    input_certs = {_normalize_fact(c) for c in (req.certifications or []) if c}
    input_schools = {_normalize_fact(e.school) for e in req.education if e.school}
    input_exp_names = {_normalize_fact(e.name) for e in req.experiences if e.name}

    added_skills = []
    for s in (result.skills or []):
        sn = _normalize_fact(s)
        if not sn:
            continue
        if sn in input_skills or sn in input_norm:
            continue
        # 子串匹配：LLM 常把技能扩展成描述（如「Go」→「精通 Go，熟悉嵌入式…」），
        # 只要描述里包含用户填过的技能（长度 >= 2，避免单字符「C」误匹配），就不算编造
        if any(len(skill) >= 2 and skill in sn for skill in input_skills):
            continue
        added_skills.append(s)

    added_certs = []
    for c in (result.certifications or []):
        cn = _normalize_fact(c)
        if not cn:
            continue
        if cn in input_certs or cn in input_norm:
            continue
        added_certs.append(c)

    added_contacts = []
    for key in ("phone", "email", "github"):
        gen = (result.contact.get(key) or "").strip()
        inp = (getattr(req, key, None) or "").strip()
        if gen and gen != inp:
            added_contacts.append(gen)

    # 学校编造检测：生成的学校必须能匹配用户填写的某个学校（双向子串容错「XX大学XX学院」）
    added_schools = []
    for edu in (result.education or []):
        sn = _normalize_fact((edu.get("school") or "").strip() if isinstance(edu, dict) else "")
        if not sn:
            continue
        if not input_schools or not any(sn in s or s in sn for s in input_schools):
            added_schools.append(edu.get("school") if isinstance(edu, dict) else edu)

    # 公司/项目名编造检测：生成的公司/项目名必须能匹配用户填写的某个名称
    added_companies = []
    for exp in (result.experiences or []):
        nn = _normalize_fact((exp.get("name") or "").strip() if isinstance(exp, dict) else "")
        if not nn:
            continue
        if not input_exp_names or not any(nn in s or s in nn for s in input_exp_names):
            added_companies.append(exp.get("name") if isinstance(exp, dict) else exp)

    passed = (
        not added_skills
        and not added_certs
        and not added_contacts
        and not added_schools
        and not added_companies
    )
    return {
        "passed": passed,
        "added_skills": added_skills,
        "added_certifications": added_certs,
        "added_contacts": added_contacts,
        "added_schools": added_schools,
        "added_companies": added_companies,
    }


async def _generate_payload(
    req: ResumeGenerateRequest,
    user: User,
    jd_prompt: str,
    reference: str,
    extra_hint: str = "",
    target_length: int = 800,
) -> dict:
    """调用 LLM 生成结构化简历 JSON。extra_hint 用于重试时追加补充约束。"""
    llm = get_user_llm_or_raise(user)
    content = build_user_prompt(
        name=req.name,
        position=req.position,
        phone=req.phone or "",
        email=req.email or "",
        github=req.github or "",
        summary=req.summary or "",
        education=req.education,
        experiences=req.experiences,
        skills=req.skills,
        certifications=req.certifications,
        jd_text=jd_prompt,
        reference=reference,
        target_length=target_length,
    )
    if extra_hint:
        content += "\n\n" + extra_hint
    response = await ainvoke_llm(llm, [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=content),
    ])
    return json.loads(_clean_json_response(response.content))


async def generate_resume(
    req: ResumeGenerateRequest,
    user: User,
    jd: Optional[JobDescription] = None,
    db: Optional[Session] = None,
) -> GenerationResult:
    jd_prompt = req.jd_text or (jd.raw_text if jd else "") or ""

    # 输入充足度评估：由代码决定字数目标，信息不足时降低目标、避免硬凑编造
    sufficiency = assess_input_sufficiency(req)
    target_length = int(sufficiency["target_length"])
    min_length = int(sufficiency["min_length"])

    # few-shot 参考：从共享语料库语义检索同方向优秀简历（向量 + BM25 混合检索，失败静默降级）
    reference = ""
    try:
        from app.modules.corpus.services import retrieve_similar
        query = " ".join(filter(None, [req.position, req.direction or "", req.summary or ""]))
        similar = await run_sync(
            retrieve_similar,
            query,
            top_k=2,
            item_type="resume",
            db=db,
            viewer_user_id=getattr(user, "id", None),
            include_private=True,
        )
        if similar:
            reference = "\n\n".join(s["raw_text"][:800] for s in similar)
    except Exception as e:  # noqa: BLE001
        logger.warning("语料检索失败（不影响生成）: %s", e)

    try:
        with trace_operation("resume_generate", getattr(user, "id", None)):
            payload = await _generate_payload(req, user, jd_prompt, reference, target_length=target_length)
    except ValueError as exc:
        logger.warning("简历生成失败: %s", exc)
        return GenerationResult(error=str(exc))
    except Exception as exc:
        logger.error("简历生成异常: %s", exc)
        return GenerationResult(error="简历生成失败，请稍后重试")

    # 教育经历用用户输入原样回填，杜绝 LLM 编造学校/学历（纯事实，不做表达优化）
    edu_input = [
        _edu_input_to_dict(e) for e in req.education
        if (e.school or e.major or e.degree or e.start or e.end)
    ]
    # 经历的事实字段（公司/项目名、角色、时间）同样回填，只让 LLM 优化 bullets
    exp_input = [_exp_input_to_dict(e) for e in req.experiences]
    # 姓名 / 联系方式同理：这三项是硬事实，用户填了就用用户的（见 `_build_result`）
    identity_input = {
        "name": (req.name or "").strip(),
        "phone": (req.phone or "").strip(),
        "email": (req.email or "").strip(),
        "github": (req.github or "").strip(),
    }
    result = await _build_result(
        payload, req.position, jd, req.jd_text or "", user,
        edu_input, exp_input, identity_input,
    )

    # 字数硬约束：低于 min_length 字则带明确反馈重新生成一次（阈值由输入充足度动态决定）
    if not result.error and len(result.resume_text) < min_length:
        logger.warning("简历生成字数不足(%s 字)，触发扩充重试", len(result.resume_text))
        try:
            with trace_operation("resume_generate_retry", getattr(user, "id", None)):
                hint = (
                    f"你上一版全文只有 {len(result.resume_text)} 字，未达到 {min_length} 字下限。"
                    f"请重新生成，全文必须达到 {target_length} 字左右（绝不能少于 {min_length} 字）。"
                    "硬性要求：每段经历必须写满 5 条要点；每条要点不少于 40 字，"
                    "写足「动词+对象+方法/技术手段+量化结果」四要素；个人总结写 3~4 句、不少于 80 字。"
                    "可以充分展开技术细节、实现过程、优化手段，但不得编造不存在的数字、技能、项目。"
                )
                payload2 = await _generate_payload(req, user, jd_prompt, reference, extra_hint=hint, target_length=target_length)
            result2 = await _build_result(
                payload2, req.position, jd, req.jd_text or "", user,
                edu_input, exp_input, identity_input,
            )
            # 采纳更长的结果（即使仍未达标，也保留更接近下限的一版）
            if not result2.error and len(result2.resume_text) > len(result.resume_text):
                result = result2
                result.tips.append("内容偏少，已自动扩充")
        except Exception as exc:
            logger.warning("字数不足扩充重试失败: %s", exc)

    # 事实保真校验（字段级）：技能/证书/联系方式/学校/公司名必须来自用户输入
    if not result.error:
        result.fidelity = check_generation_fidelity(req, result)

    # bullet 级事实保真：数字/年份/角色/技术栈必须可溯源，检出即带定位反馈重写一次
    if not result.error:
        result = await _bullet_fidelity_gate(
            req, user, result, jd, jd_prompt, reference, edu_input, exp_input,
            target_length=target_length,
        )

    # 质量评分 + 质量重试：把质量铁律从 prompt 落到代码规则，不达标带反馈重写
    if not result.error:
        result = await _quality_gate(req, user, result, jd, jd_prompt, reference, edu_input, exp_input)

    # 质量重写可能把整版结果换掉（fidelity/tips 一并丢失）→ 用最终版本重算报告。
    # 报告在净化**之前**算，才能留下"检出过哪些数字"的证据。
    if not result.error:
        result.fidelity = check_generation_fidelity(req, result)
        # 检出即处置：用户没写过的技能 / 证书 / 联系方式就地剔除。
        # 只上报不处置等于没做护栏 —— 之前这两个字段确实没被任何分支消费过。
        result = _strip_fabricated_facts(
            req, result, result.fidelity, jd_prompt, req.direction or ""
        )
        _attach_bullet_fidelity(result, _req_input_text(req), _source_roles(req))
        if reference:
            # 标注范文参考（对标标书 materialSources）：让用户知道写法参考了同方向范文
            result.reference_source = f"{req.direction or '通用'}方向优秀简历范文"

    # 确定性收尾：质量重写可能又把数字加回来 → 必须在所有 LLM 调用之后做硬兜底
    if not result.error:
        result = _apply_number_sanitize(req, result, jd_prompt)

    # 一页硬约束（最后一道闸）：净化与质量重算之后仍超长 → 二次精简；仍超长则报错拒落库
    if not result.error:
        result = await _enforce_one_page(
            result, _req_input_text(req), user, jd_prompt, req.direction or ""
        )

    # 输入充足度报告（供前端提示补充信息）
    if not result.error:
        result.sufficiency = sufficiency
        # AI 语义评分（正则判不了的软质量，失败静默降级）；同步调用移出事件循环
        result.semantic = await run_sync(_semantic_gate, user, result, jd_prompt)

    return result


def _semantic_gate(user: User, result: GenerationResult, jd_prompt: str) -> Dict[str, Any]:
    """AI 语义评分：对正则判不了的语义质量维度打分，失败静默降级返回 {}。"""
    try:
        llm = get_user_llm_or_raise(user)
        structured = {
            "position": result.position,
            "skills": result.skills,
        }
        return score_semantic_quality(llm, structured, result.resume_text, jd_prompt) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("语义评分失败（不影响生成）: %s", exc)
        return {}


def _structured_from_result(result: GenerationResult) -> Dict[str, Any]:
    """从 GenerationResult 还原结构化 dict（experiences 是同一个 list 对象，
    因此 fidelity 的就地修改会同步回 result）。"""
    return {
        "name": result.name,
        "position": result.position,
        "contact": result.contact,
        "summary": result.summary,
        "education": result.education,
        "experiences": result.experiences,
        "skills": result.skills,
        "certifications": result.certifications,
    }


def _score_result_quality(
    result: GenerationResult, jd_prompt: str, direction: str = ""
):
    """从 GenerationResult 重建结构化 dict，做质量评分（不触发重写）。"""
    return score_resume_quality(
        _structured_from_result(result),
        result.resume_text,
        jd_prompt,
        direction or "",
        is_too_long=_estimate_too_long("", result.resume_text),
    )


def _attach_quality(result: GenerationResult, jd_prompt: str, direction: str = "") -> GenerationResult:
    """给 result 附上质量评分报告（供逐段重生成/保存等不触发重写的场景复用）。"""
    if not result.error:
        result.quality = report_to_dict(_score_result_quality(result, jd_prompt, direction))
    return result


def _source_roles(req: ResumeGenerateRequest) -> List[str]:
    return [e.role or "" for e in req.experiences]


def _structured_source_text(structured: Dict[str, Any]) -> str:
    """把前端传来的 structured 整体序列化成"事实来源"文本（逐段重写路径用）。"""
    return json.dumps(structured or {}, ensure_ascii=False)


def _structured_roles(structured: Dict[str, Any]) -> List[str]:
    return [
        str(e.get("role") or "")
        for e in (structured.get("experiences") or [])
        if isinstance(e, dict)
    ]


async def _bullet_fidelity_gate(
    req: ResumeGenerateRequest,
    user: User,
    result: GenerationResult,
    jd: Optional[JobDescription],
    jd_prompt: str,
    reference: str,
    edu_input: List[Dict[str, Any]],
    exp_input: List[Dict[str, str]],
    target_length: int = 800,
) -> GenerationResult:
    """bullet 级事实保真：数字/年份/角色/技术栈必须能在用户输入里找到出处。

    检出硬违规（编造数字、编造年份）时，把「具体哪一条、哪一个数字」的定位反馈
    喂回去重写一次，取违规更少的一版；LLM 若仍不听话，由 `_apply_number_sanitize`
    在最后做确定性兜底。

    ``target_length`` **必须透传**：重试时不传会回落到默认 800 字，而
    ``sufficiency`` 给"信息少"的用户的目标是 400~500 字 —— 正好把用户
    往"凑字数"的方向推，反而放大编造风险（字数重试那条路径本来就传了）。
    """
    source_text = _req_input_text(req)
    roles = _source_roles(req)
    report = check_bullet_fidelity(_structured_from_result(result), source_text, roles)

    if report.hard_count:
        logger.warning("简历事实保真不通过（%s 处编造数字/时间），触发重写", report.hard_count)
        identity_input = {
            "name": (req.name or "").strip(),
            "phone": (req.phone or "").strip(),
            "email": (req.email or "").strip(),
            "github": (req.github or "").strip(),
        }
        try:
            with trace_operation("resume_generate_fidelity_retry", getattr(user, "id", None)):
                payload2 = await _generate_payload(
                    req, user, jd_prompt, reference,
                    extra_hint=report.feedback_hint, target_length=target_length,
                )
            result2 = await _build_result(
                payload2, req.position, jd, req.jd_text or "", user,
                edu_input, exp_input, identity_input,
            )
            if not result2.error:
                report2 = check_bullet_fidelity(
                    _structured_from_result(result2), source_text, roles
                )
                if report2.hard_count < report.hard_count:
                    result = result2
                    report = report2
                    result.fidelity = check_generation_fidelity(req, result)
                    result.tips.append("已按事实保真规则重写（删除原始信息里没有的数字）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("事实保真重写失败: %s", exc)

    result.fidelity = dict(result.fidelity or {})
    result.fidelity["bullets"] = report.to_dict()
    return result


def _sanitize_numbers_with_source(
    result: GenerationResult,
    source_text: str,
    jd_prompt: str,
    direction: str = "",
) -> GenerationResult:
    """确定性收尾：把产物里所有「source_text 里没有」的数字就地摘掉。

    这一步**不依赖 LLM 是否听话** —— 质量重写有可能把数字又加回来，
    所以必须放在所有 LLM 调用之后执行，作为硬约束而不是建议。
    """
    structured = _structured_from_result(result)
    _, removed = sanitize_structured(structured, source_text)
    if not removed:
        return result

    result.summary = structured.get("summary", result.summary)
    result.experiences = structured.get("experiences") or result.experiences
    # structured 是唯一真源：文本一律由它重新渲染（删数字后文本必须同步跟着变），
    # 否则又会出现"结构化数据与文本不一致 → 导出与界面不一致"的老问题
    rendered = render_resume_text(_structured_from_result(result))
    if rendered:
        result.resume_text = rendered

    joined = "、".join(removed[:8])
    result.tips.append(f"已按事实保真删除 {len(removed)} 处原始信息中不存在的数字：{joined}")
    result.risks.append({
        "section": "experiences",
        "level": "warn",
        "issue": f"检测到 {len(removed)} 处未在原始信息中出现的数字，已自动删除：{joined}",
        "suggestion": "如果这些成果确实存在，请把真实数字补进「原始信息」后重新生成；否则保持删除",
    })

    result.sections = list_sections(_structured_from_result(result))
    result.quality = report_to_dict(
        _score_result_quality(result, jd_prompt, direction)
    )
    logger.info("事实保真收尾：删除 %s 处可疑数字", len(removed))
    return result


def _apply_number_sanitize(
    req: ResumeGenerateRequest,
    result: GenerationResult,
    jd_prompt: str,
) -> GenerationResult:
    """生成主流程用的收尾：事实来源 = 用户填写的原始信息（不含 JD）。"""
    return _sanitize_numbers_with_source(result, _req_input_text(req), jd_prompt, req.direction or "")


def _apply_structured(result: GenerationResult, structured: Dict[str, Any]) -> None:
    """把重建后的 structured 写回 result（键存在才写，避免误清空字段）。"""
    if structured.get("name"):
        result.name = structured["name"]
    if structured.get("position"):
        result.position = structured["position"]
    if "contact" in structured and structured["contact"]:
        result.contact = structured["contact"]
    if "summary" in structured:
        result.summary = structured["summary"] or ""
    for field_name in ("education", "experiences", "skills", "certifications"):
        if field_name in structured:
            setattr(result, field_name, structured[field_name] or [])


async def _enforce_one_page(
    result: GenerationResult,
    source_text: str,
    user: User,
    jd_prompt: str,
    direction: str = "",
) -> GenerationResult:
    """一页硬约束的**最后一道闸**：生成 / 逐段重写 / 保存三条路径都必须在末尾调用。

    放在事实保真净化与质量重算**之后**，理由：
    - 精简会再次调用 LLM，可能把数字又加回来 → 必须重跑数字净化；
    - 精简改动了要点数量与分节 → sections 与质量分必须重算，不能沿用精简前的报告。

    若两次精简后仍超一页 → **fail closed**：置 `result.error`（调用方据此跳过落库），
    避免"界面显示一页、导出却是两页"这种最坏情况。
    """
    if result.error:
        return result
    if not _one_page_exceeded(result.resume_text):
        return result

    logger.warning("简历超出字符上限(%s 字)，触发二次精简", len(result.resume_text))
    condensed, removed = await _try_condense(result.resume_text, user)
    if condensed:
        rebuilt = restructure_from_text(_structured_from_result(result), condensed)
        rebuilt_text = render_resume_text(rebuilt)
        # 只有确实变短才采纳（LLM 原样返回时不要白跑一遍净化/评分）
        if rebuilt_text and len(rebuilt_text) < len(result.resume_text):
            _apply_structured(result, rebuilt)
            result.resume_text = rebuilt_text
            result.tips.append("内容仍超出，已二次精简")
            if removed:
                result.tips.append("二次精简内容：" + "、".join(removed))
            # 精简后必须重跑：数字净化 → sections → 质量评分
            result = _sanitize_numbers_with_source(result, source_text, jd_prompt, direction)
            result.sections = list_sections(_structured_from_result(result))
            result = _attach_quality(result, jd_prompt, direction)

    if _one_page_exceeded(result.resume_text):
        result.error = (
            f"简历内容超过一页（{len(result.resume_text)} 字，上限 {_ONE_PAGE_MAX_CHARS} 字），"
            "自动精简后仍放不下。请减少一段经历或精简内容后重试。"
        )
        logger.warning("一页约束未满足，已拒绝落库: %s 字", len(result.resume_text))
    return result


def _attach_bullet_fidelity(
    result: GenerationResult,
    source_text: str,
    source_roles: Optional[List[str]] = None,
) -> GenerationResult:
    """只报不改：给逐段重写/保存等路径附上 bullet 保真报告。

    source_text 用**用户手上已有的那份 structured**：LLM 若原样保留旧数字就不算新增，
    只有凭空冒出新数字才报 —— 这正是我们要的信息。
    """
    if result.error:
        return result
    report = check_bullet_fidelity(_structured_from_result(result), source_text, source_roles or [])
    result.fidelity = dict(result.fidelity or {})
    result.fidelity["bullets"] = report.to_dict()
    return result


def _strip_fabricated_facts(
    req: ResumeGenerateRequest,
    result: GenerationResult,
    fidelity: Dict[str, Any],
    jd_prompt: str,
    direction: str = "",
) -> GenerationResult:
    """把 fidelity 检出的「用户没写过的技能 / 证书 / 联系方式」就地剔除。

    ⚠️ 为什么必须**处置**而不是只上报：``added_skills`` / ``added_certifications``
    此前只被赋值给 ``result.fidelity``、经路由回传前端，**全后端没有任何 if 分支
    消费它**（grep 可证）—— 于是「护栏检出了编造」和「编造照样落库」同时成立。

    为什么用确定性剔除、而不是像 hr-breaker 那样带着失败原因重新生成：
    技能 / 证书 / 联系方式是**枚举类事实**，正确答案唯一（用户填了什么就是什么），
    再问一次模型不会更准，只会换一批新的编造。数字那类**开放式事实**才适合回灌
    重写（见 `_bullet_fidelity_gate`，那条链路本来就有）。
    """
    added_skills = [str(s) for s in (fidelity.get("added_skills") or [])]
    added_certs = [str(c) for c in (fidelity.get("added_certifications") or [])]
    added_contacts = [str(c).strip() for c in (fidelity.get("added_contacts") or [])]
    if not (added_skills or added_certs or added_contacts):
        return result

    if added_skills:
        drop = {_normalize_fact(s) for s in added_skills}
        result.skills = [s for s in (result.skills or [])
                         if _normalize_fact(str(s)) not in drop]
    if added_certs:
        drop = {_normalize_fact(c) for c in added_certs}
        result.certifications = [c for c in (result.certifications or [])
                                 if _normalize_fact(str(c)) not in drop]
    if added_contacts:
        drop = set(added_contacts)
        result.contact = {
            key: value for key, value in (result.contact or {}).items()
            if str(value or "").strip() not in drop
        }

    parts: List[str] = []
    if added_skills:
        parts.append("技能：" + "、".join(added_skills[:6]))
    if added_certs:
        parts.append("证书：" + "、".join(added_certs[:6]))
    if added_contacts:
        parts.append(
            "联系方式：" + "、".join(added_contacts[:3]) + "（已清空，请自行补上真实信息）"
        )
    detail = "；".join(parts)
    result.tips.append(f"已删除原始信息里没有的内容（多由模型自行补充）：{detail}")
    result.risks.append({
        "section": "skills",
        "level": "warn",
        "issue": f"检测到你没有提供的技能/证书/联系方式，已自动删除：{detail}",
        "suggestion": "若确实具备，请把这些内容补进表单后重新生成；无出处的条目不要保留",
    })

    # structured 是唯一真源：删条目后文本必须同步重渲染，否则又会出现
    # "结构化数据与文本不一致 → 导出与界面不一致"的老问题。
    rendered = render_resume_text(_structured_from_result(result))
    if rendered:
        result.resume_text = rendered
    result.sections = list_sections(_structured_from_result(result))

    # 处置完重算一次保真报告：回传前端的应是**最终状态**的结论，同时把
    # 「删过什么」留在 ``fidelity["removed"]`` 里当证据 —— 两件事都不能丢。
    final = check_generation_fidelity(req, result)
    final["removed"] = {
        "skills": added_skills,
        "certifications": added_certs,
        "contacts": added_contacts,
    }
    result.fidelity = final
    result.quality = report_to_dict(_score_result_quality(result, jd_prompt, direction))
    logger.info(
        "事实保真处置：剔除技能 %s 个 / 证书 %s 个 / 联系方式 %s 个",
        len(added_skills), len(added_certs), len(added_contacts),
    )
    return result


async def _quality_gate(
    req: ResumeGenerateRequest,
    user: User,
    result: GenerationResult,
    jd: Optional[JobDescription],
    jd_prompt: str,
    reference: str,
    edu_input: List[Dict[str, Any]],
    exp_input: List[Dict[str, str]],
) -> GenerationResult:
    """质量评分 + 不达标重写（最多重试一次，取质量分更高的结果）。"""
    quality = _score_result_quality(result, jd_prompt, req.direction or "")
    result.quality = report_to_dict(quality)

    # 不达标就用评分器生成的反馈触发一次重写。
    # ⚠️ 条件必须与 `passed` 的判据**同源**（quality.py:365 = 总分 ≥ 75 且无硬违规）：
    # 此前写成 `not passed and total_score < 70`，于是 [70, 75) 这一段既不重试、
    # 也算不上通过 —— 成了没人管的空档。
    if not quality.passed:
        logger.warning("简历质量不达标(%s 分)，触发质量重试", quality.total_score)
        try:
            with trace_operation("resume_generate_quality_retry", getattr(user, "id", None)):
                payload2 = await _generate_payload(
                    req, user, jd_prompt, reference, extra_hint=quality.feedback_hint
                )
            result2 = await _build_result(
                payload2, req.position, jd, req.jd_text or "", user, edu_input, exp_input
            )
            if not result2.error:
                q2 = _score_result_quality(result2, jd_prompt, req.direction or "")
                if q2.total_score > quality.total_score:
                    result = result2
                    result.quality = report_to_dict(q2)
                    result.tips.append("已按质量规则自动重写优化")
        except Exception as exc:  # noqa: BLE001
            logger.warning("质量重试失败: %s", exc)

    return result


async def regenerate_section(
    structured: Dict[str, Any],
    section: str,
    index: Optional[int],
    jd_text: str,
    user: User,
    jd: Optional[JobDescription] = None,
) -> GenerationResult:
    try:
        with trace_operation("resume_regenerate_section", getattr(user, "id", None)):
            llm = get_user_llm_or_raise(user)
            response = await ainvoke_llm(llm, [
                SystemMessage(content=REGENERATE_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_regenerate_user_prompt(
                        structured,
                        section,
                        index,
                        jd_text,
                    )
                ),
            ])
            payload = json.loads(_clean_json_response(response.content))
    except ValueError as exc:
        logger.warning("逐段重新生成失败: %s", exc)
        return GenerationResult(error=str(exc))
    except Exception as exc:
        logger.error("逐段重新生成异常: %s", exc)
        return GenerationResult(error="重新生成失败，请稍后重试")

    # 模型经常只回被改的那一段（甚至整段不回），这里按"只补空"补齐成完整 structured；
    # 不做这一步的话，模型少给一个 name 就会让整次"重写这一段"失败。
    payload = _merge_regenerate_payload(payload, structured)

    # 事实来源快照：必须在 _build_result 之前取，之后 structured 会被就地改动
    source_text = _structured_source_text(structured)
    result = await _build_result(
        payload,
        str(structured.get("position") or ""),
        jd,
        jd_text,
        user,
    )
    if not result.error:
        # 事实来源 = 用户手上那份 structured：原样保留旧数字不算新增，凭空冒出新数字才算
        _attach_bullet_fidelity(result, source_text, _structured_roles(structured))
        result = _sanitize_numbers_with_source(result, source_text, jd_text)
        result = _attach_quality(result, jd_text)
        result = await _enforce_one_page(result, source_text, user, jd_text)
    return result


def _sanitize_candidate(candidate: Any, source_text: str) -> Tuple[Any, List[str]]:
    """对「改写候选」递归做数字净化，事实来源 = 用户采用前那份 structured。

    与逐段重写路径同一口径：原样保留旧数字不算新增，凭空冒出新数字才算。
    候选形状不固定（summary/skills 是字符串或数组，经历是 dict），所以递归遍历，
    只动字符串叶子。
    """
    allowed = canonical_numbers(source_text)
    removed: List[str] = []

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            new_text, hits = sanitize_text(value, allowed)
            removed.extend(hits)
            return new_text
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(candidate), list(dict.fromkeys(removed))


async def regenerate_section_variants(
    structured: Dict[str, Any],
    section: str,
    index: Optional[int],
    jd_text: str,
    user: User,
    jd: Optional[JobDescription] = None,
) -> List[Any]:
    """为指定字段生成 3 个改写候选，供用户选择（而非覆盖式重写）。"""
    jd_prompt = jd_text or (jd.raw_text if jd else "") or ""
    try:
        with trace_operation("resume_regenerate_variants", getattr(user, "id", None)):
            llm = get_user_llm_or_raise(user)
            response = await ainvoke_llm(llm, [
                SystemMessage(content=REGENERATE_VARIANTS_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_regenerate_user_prompt(
                        structured, section, index, jd_prompt,
                    )
                ),
            ])
            payload = json.loads(_clean_json_response(response.content))
    except Exception as exc:
        logger.warning("生成改写候选失败: %s", exc)
        return []

    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        return []

    # ⚠️ 这里此前是**唯一零护栏**的路径：候选直接 ``return``，用户点「采用候选」后
    # 走 `save_structured` 落库，而那条路自述"不做 bullet 保真"（它的理由是
    # "把关已在生成 / 逐段重写两步做过" —— 候选这条路**恰恰没经过那两步**）。
    # 于是模型在候选里编造的百分比 / 规模数字可以一路进库。
    # 事实来源 = 采用前那份 ``structured``：旧数字保留不算新增，新数字才算。
    source_text = _structured_source_text(structured)
    cleaned: List[Any] = []
    for candidate in candidates:
        if candidate is None:
            continue
        fixed, removed = _sanitize_candidate(candidate, source_text)
        if removed:
            logger.info(
                "候选数字净化：删除 %s 处无出处数字 %s", len(removed), removed[:5]
            )
        cleaned.append(fixed)
    return cleaned[:3]


async def save_structured(
    structured: Dict[str, Any],
    jd_text: str,
    user: User,
    jd: Optional[JobDescription] = None,
) -> GenerationResult:
    """把前端选定候选后的完整 structured 重新渲染 + 评分，用于落库（不重新调 LLM）。

    这里**不做 bullet 保真校验**：事实来源就是 structured 自己（用户已确认的最终版），
    拿它验它必然全部通过；若用户手工改过某个数字，那是用户的输入，不该被自动删掉。
    把关的位置因此全在前端到达这里之前的三条路上：
    ① 生成（`_bullet_fidelity_gate` + `_apply_number_sanitize` + `_strip_fabricated_facts`）；
    ② 逐段重写（`_sanitize_numbers_with_source`）；
    ③ **候选变体（`regenerate_section_variants` 里的 `_sanitize_candidate`）** ——
    这一条曾经缺失：候选是唯一直接回给前端、又绕过①②的 LLM 产物，
    补上之后「选候选 → 保存」才不再是一条零护栏的落库通道。

    但**一页硬约束必须做**：保存是落库前的最后一站，超长在这里放行就会出现
    "界面一页、导出两页"。命中时会把产物精简回一页；精简不出来则报错，由路由拒绝落库。
    """
    # 事实来源快照：必须在 _build_result 之前取（之后 structured 会被就地改动）
    source_text = _structured_source_text(structured)
    result = await _build_result(
        structured,
        str(structured.get("position") or ""),
        jd,
        jd_text,
        user,
    )
    result = _attach_quality(result, jd_text)
    if not result.error:
        result = await _enforce_one_page(result, source_text, user, jd_text)
    return result
