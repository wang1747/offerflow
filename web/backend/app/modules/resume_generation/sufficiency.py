"""输入信息充足度评估：把「信息不充足时的美化/扩充」从 prompt 约束落到代码规范。

评估用户填写的经历/教育/技能/联系方式/JD 的信息量，输出：
- score：0-100 充足度分
- level：sufficient / moderate / insufficient
- target_length / min_length：动态字数目标（信息越少目标越低，避免硬凑编造）
- gaps：缺失/薄弱的项，用于前端提示补充

设计原则（与 quality.py 一致）：
1. 纯函数、零 LLM 依赖，可单测、可复用。
2. 字数目标由代码根据信息量决定，而不是 prompt 里写死 700~900 字。
   信息不足时降低目标，宁可短而真，不长而编。
"""

from typing import Any, Dict, List, Optional

# 充足度 → (目标字数, 最低字数)：最低字数用于触发扩充重试的阈值
_LEVEL_CONFIG = {
    "sufficient":   {"target_length": 800, "min_length": 700},
    "moderate":     {"target_length": 650, "min_length": 550},
    "insufficient": {"target_length": 500, "min_length": 400},
}


def assess_input_sufficiency(req: Any) -> Dict[str, Any]:
    """评估输入充足度，返回报告 dict。

    req 需具备 fields：experiences（含 name/bullets）、education（含 school/major）、
    skills、certifications、phone、email、jd_text、summary。
    """
    experiences = [e for e in (req.experiences or []) if (e.name or "").strip()]
    bullet_count = 0
    bullet_chars = 0
    for e in experiences:
        bullets = [b for b in (e.bullets or []) if b.strip()]
        bullet_count += len(bullets)
        bullet_chars += sum(len(b) for b in bullets)

    education = [e for e in (req.education or [])
                 if (e.school or "").strip() or (e.major or "").strip()]
    skills = [s for s in (req.skills or []) if s.strip()]
    certs = [c for c in (req.certifications or []) if c.strip()]
    has_contact = bool((req.phone or "").strip() or (req.email or "").strip())
    has_jd = bool((req.jd_text or "").strip())
    has_summary = bool((req.summary or "").strip())

    score = 0
    gaps: List[str] = []

    # 经历（权重最高，35 分）
    if experiences:
        score += 15
        if bullet_count >= 3:
            score += 10
        else:
            gaps.append("经历要点偏少（不足 3 条），建议补充「做了什么、怎么做的、结果」")
        avg = bullet_chars / max(bullet_count, 1)
        if avg >= 15:
            score += 10
        else:
            gaps.append("要点描述偏简略（平均每条不足 15 字），建议展开技术细节与成果")
    else:
        gaps.append("缺少项目/实习/工作经历")

    # 教育（15 分）
    if education:
        score += 15
    else:
        gaps.append("缺少教育经历")

    # 技能（15 分）
    if skills:
        score += 15
    else:
        gaps.append("缺少技能")

    # 联系方式（10 分）
    if has_contact:
        score += 10
    else:
        gaps.append("缺少手机号/邮箱联系方式")

    # 目标 JD（15 分）
    if has_jd:
        score += 15
    else:
        gaps.append("未填写目标 JD（无法做关键词对齐）")

    # 总结 / 证书（各 5 分）
    if has_summary:
        score += 5
    if certs:
        score += 5

    score = max(0, min(100, score))
    if score >= 75:
        level = "sufficient"
    elif score >= 50:
        level = "moderate"
    else:
        level = "insufficient"

    cfg = _LEVEL_CONFIG[level]
    message = {
        "sufficient": "信息充足，按标准篇幅生成",
        "moderate": "信息中等，已按精简篇幅生成；补充更多细节可让简历更充实",
        "insufficient": "信息偏少，已按精简篇幅生成（宁短勿编）；强烈建议补充经历要点、技能与目标 JD",
    }[level]

    return {
        "score": score,
        "level": level,
        "target_length": cfg["target_length"],
        "min_length": cfg["min_length"],
        "gaps": gaps,
        "message": message,
    }
