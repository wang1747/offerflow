"""ATS 兼容性检查工具（技能化重写版）。

旧版的问题：把「所有 ≥3 字符的英文词」当关键词做覆盖率，得到的分数是噪音。
新版基于技能词典 + JD 需求词做精确命中，输出有意义的 matched / missing。
"""

import re
from typing import Dict, List, Set

from app.core.text_utils import SKILL_ALIASES, extract_skills, normalize_text


def _match_skill_in_text(skill: str, text_norm: str) -> bool:
    aliases = SKILL_ALIASES.get(skill, [skill])
    for alias in aliases:
        if normalize_text(alias).strip() and normalize_text(alias) in text_norm:
            return True
    return False


def _jd_requirements(jd_text: str) -> List[str]:
    """从 JD 文本提取需求词：技能词典命中的词 → 英文技术词（去重）。"""
    reqs: List[str] = []
    seen: Set[str] = set()

    # 1. 技能词典命中的技能（含中英文）
    for skill in extract_skills(jd_text):
        key = normalize_text(skill)
        if key not in seen:
            reqs.append(skill)
            seen.add(key)

    # 2. 英文技术词（过滤连接词 + normalize 去重，避免 FastAPI/fastapi 重复）
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

    # 控制数量，避免噪音
    return reqs[:60]


def check_ats_compatibility(resume_text: str, jd_text: str = "") -> Dict:
    """检查简历的 ATS 兼容性：联系方式、分节、长度、JD 关键词覆盖。"""
    text = resume_text or ""
    text_norm = normalize_text(text)
    issues: List[str] = []
    suggestions: List[str] = []
    score = 100

    # 1. 联系方式
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

    # 2. 分节
    section_names = ["教育", "项目", "经验", "技能", "经历", "自我评价", "实习", "工作"]
    found_sections = [name for name in section_names if name in text]
    if not found_sections:
        score -= 20
        issues.append("未识别到常见简历分节（教育/项目/经验/技能）")
    elif len(found_sections) < 2:
        score -= 10
        issues.append("简历分节较少，建议补充教育、项目或技能模块")

    # 3. 长度
    if len(text) < 200:
        score -= 15
        issues.append("内容过短，可能信息不足")
    long_lines = [line for line in text.splitlines() if len(line) > 200]
    if long_lines:
        score -= 10
        issues.append("存在超长段落，建议拆分为短句和项目符号")
        suggestions.append("将大段描述改为一句话要点 + 数据化成果")

    # 4. JD 关键词覆盖（技能化，有意义的命中/缺失）
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
            # 按覆盖率连续扣分，避免「技术岗关键词全命中就 100 分」的虚高
            if coverage < 1.0:
                score -= int((1 - coverage) * 30)
            if coverage < 0.5:
                issues.append(
                    f"JD 关键词命中率仅 {int(coverage * 100)}%，ATS 可能无法识别相关技能"
                )
                suggestions.append(
                    "补充 JD 中的关键词：" + "、".join(missing[:8])
                )

    if not suggestions and issues:
        suggestions.append("按 教育 → 技能 → 项目/实习 → 自我评价 的顺序整理简历")

    return {
        "score": max(0, min(100, score)),
        "issues": issues,
        "suggestions": suggestions,
        "matched_keywords": matched,
        "missing_keywords": missing,
    }


def extract_keywords(text: str) -> List[str]:
    """从文本中提取候选关键词（技能词典优先）。"""
    skills = list(extract_skills(text))
    extra = [
        kw
        for kw in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]*", (text or "").lower())
        if len(kw) >= 3 and kw not in {s.lower() for s in skills}
    ]
    return list(dict.fromkeys(skills + extra))
