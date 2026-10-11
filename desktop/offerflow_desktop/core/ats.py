"""ATS 兼容性检查（自 Web 版移植：技能化关键词命中，规则计分）。"""

import re
from typing import Any, Dict, List, Optional

from .sections import (
    RECOMMENDED_SECTION_ORDER,
    is_ats_recommended,
    section_labels_in_order,
)
from .text_utils import SKILL_ALIASES, alias_in_text, extract_skills, normalize_text


def _match_skill_in_text(skill: str, text_norm: str) -> bool:
    aliases = SKILL_ALIASES.get(skill, [skill])
    for alias in aliases:
        norm = normalize_text(alias).strip()
        if alias_in_text(norm, text_norm):
            return True
    return False


def _jd_requirements(jd_text: str) -> List[str]:
    """从 JD 文本提取需求词：技能词典命中的词 → 英文技术词（去重）。"""
    reqs: List[str] = []
    seen = set()
    for skill in extract_skills(jd_text):
        key = normalize_text(skill)
        if key not in seen:
            reqs.append(skill)
            seen.add(key)
    stop = {
        "the", "and", "you", "are", "for", "with", "have", "will", "that",
        "this", "our", "your", "from", "who", "what", "when", "where",
        "experience", "years", "ability", "team", "work", "job", "role",
        "plus", "good", "strong", "skills", "knowledge", "familiar",
        "preferred", "required", "must", "nice", "etc", "also", "other",
    }
    for kw in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{1,}", jd_text.lower()):
        if len(kw) >= 3 and kw not in stop:
            key = normalize_text(kw)
            if key not in seen:
                reqs.append(kw)
                seen.add(key)
    return reqs[:60]


def check_ats_compatibility(resume_text: str, jd_text: str = "",
                            template: Optional[Any] = None,
                            section_order: Optional[Any] = None) -> Dict:
    """检查简历的 ATS 兼容性：联系方式、分节、长度、JD 关键词覆盖。

    ``template`` 是**鸭子类型**的（只读它的 ``ats_notes`` / ``label`` / ``columns``），
    刻意不 import services —— core 层不依赖 services，否则就分层倒置了。
    调用方（services 层）负责把「已经叠过外观」的有效模板传进来，
    这样字号被用户缩小到 9pt 以下时，这里能自动多报一条风险。

    ``section_order`` 是分区顺序（``core.sections.BODY_SECTIONS`` 的排列）。
    传了它，这里的建议才会跟用户实际排的顺序对上；不传则退回通用建议。

    **分数只反映内容问题，不因版式扣分**：行距 1.08 属于我们提供的模板设计，
    记到用户头上不合理；模板带来的风险一律只如实列出（``template_notes``），
    让用户自己决定换版式还是调字号。
    """
    text = resume_text or ""
    text_norm = normalize_text(text)
    issues: List[str] = []
    suggestions: List[str] = []
    score = 100

    has_email = bool(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
    has_phone = bool(re.search(r"1[3-9]\d{9}", text)) or bool(
        re.search(r"\+?\d[\d\s-]{7,}\d", text)
    )
    if not has_email:
        score -= 15
        issues.append("缺少邮箱联系方式")
    if not has_phone:
        score -= 10
        issues.append("缺少手机号联系方式")

    section_names = ["教育", "项目", "经验", "技能", "经历", "自我评价", "实习", "工作"]
    found_sections = [name for name in section_names if name in text]
    if not found_sections:
        score -= 20
        issues.append("未识别到常见简历分节（教育/项目/经验/技能）")
    elif len(found_sections) < 2:
        score -= 10
        issues.append("简历分节较少，建议补充教育、项目或技能模块")

    if len(text) < 200:
        score -= 15
        issues.append("内容过短，可能信息不足")
    long_lines = [line for line in text.splitlines() if len(line) > 200]
    if long_lines:
        score -= 10
        issues.append("存在超长段落，建议拆分为短句和项目符号")
        suggestions.append("将大段描述改为一句话要点 + 数据化成果")

    # ---- 版式（模板）带来的可读性风险：只报不改分 ----
    template_notes: List[str] = []
    if template is not None:
        label = str(getattr(template, "label", "")
                    or getattr(template, "key", "") or "")
        raw_notes = getattr(template, "ats_notes", None) or []
        for note in raw_notes:
            line = str(note).strip()
            if not line:
                continue
            template_notes.append(line)
            issues.append(f"当前版式「{label}」{line}" if label else f"当前版式{line}")
        if template_notes:
            suggestions.append(
                "换一排版式，或在上面的外观设置里调大字号、放宽行距"
            )

    matched: List[str] = []
    missing: List[str] = []
    if jd_text:
        reqs = _jd_requirements(jd_text)
        if reqs:
            for req in reqs:
                if _match_skill_in_text(req, text_norm):
                    matched.append(req)
                else:
                    missing.append(req)
            coverage = len(matched) / len(reqs)
            if coverage < 1.0:
                score -= int((1 - coverage) * 30)
            if coverage < 0.5:
                issues.append(f"JD 关键词命中率仅 {int(coverage * 100)}%，ATS 可能无法识别相关技能")
                suggestions.append("补充 JD 中的关键词：" + "、".join(missing[:8]))

    # ---- 分区顺序：只在用户**显式设过**顺序、且与推荐顺序不同时才提 ----
    # 没设过就不提。默认顺序（summary 在前）与推荐顺序本来就不一样，
    # 要是每次都拿这个去说事，就成了一句永远刷屏的废话 ——
    # 用户还没做过任何选择就被「建议调整」，只会让建议列表失去可信度。
    explicit_order = section_order if section_order else None
    if explicit_order and not is_ats_recommended(explicit_order):
        suggestions.append(
            "小节顺序为 " + " → ".join(section_labels_in_order(explicit_order))
            + "；建议调整为 " + " → ".join(
                section_labels_in_order(RECOMMENDED_SECTION_ORDER))
            + "，让技能类关键词集中成块"
        )
    if not suggestions and issues:
        suggestions.append(
            "按 " + " → ".join(section_labels_in_order(RECOMMENDED_SECTION_ORDER))
            + " 的顺序整理简历"
        )

    return {
        "score": max(0, min(100, score)),
        "issues": issues,
        "suggestions": suggestions,
        "matched_keywords": matched,
        "missing_keywords": missing,
        "template_notes": template_notes,
    }
