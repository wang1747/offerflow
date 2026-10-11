"""输入信息充足度评估（自服务端 `modules/resume_generation/sufficiency.py` 移植）。

把「信息不充足时的美化/扩充」从 prompt 约束落到代码规范：评估用户填写的
经历 / 教育 / 技能 / 联系方式 / JD 的信息量，输出：

- ``score``：0-100 充足度分
- ``level``：sufficient / moderate / insufficient
- ``target_length`` / ``min_length``：**动态字数目标**（信息越少目标越低，避免硬凑编造）
- ``gaps``：缺失 / 薄弱的项，用于前端提示补充

设计原则（与服务端 ``quality.py`` 一致）：
1. 纯函数、零 LLM 依赖，可单测、可复用。
2. 字数目标由代码根据信息量决定，而不是 prompt 里写死 700~900 字。
   信息不足时降低目标，**宁可短而真，不长而编**。

⚠️ 两份同源声明
---------------
服务端同一份在 ``backend/app/modules/resume_generation/sufficiency.py``。
两个仓库无法共享代码、只能共享判据，**规则改动必须两边一起改**。
判据由 ``tests/test_sufficiency_parity.py`` 锁死（权重 / 阈值 / 字数目标）。

桌面侧唯一差异：服务端 ``req`` 是 pydantic 对象（``req.experiences``），
桌面传的是 dict（``_collect_request()`` 的返回值）—— 故此处统一用 ``_get()``
兼容「dict 与对象」两种形状，**判据逐字与服务端对齐**。
"""

from typing import Any, Dict, List

# 充足度 -> (目标字数, 最低字数)：最低字数用于触发扩充重试的阈值
_LEVEL_CONFIG: Dict[str, Dict[str, int]] = {
    "sufficient": {"target_length": 800, "min_length": 700},
    "moderate": {"target_length": 650, "min_length": 550},
    "insufficient": {"target_length": 500, "min_length": 400},
}

# 权重（用于判据测试引用，未经测试不得改动）
W_EXPERIENCE = 15
W_BULLETS = 10
W_BULLET_DETAIL = 10
W_EDUCATION = 15
W_SKILLS = 15
W_CONTACT = 10
W_JD = 15
W_SUMMARY = 5
W_CERTS = 5

# 分档阈值
LEVEL_SUFFICIENT = 75
LEVEL_MODERATE = 50

# 要点判据
MIN_BULLET_COUNT = 3
MIN_BULLET_AVG_CHARS = 15


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与对象两种形状（服务端传 pydantic，桌面传 dict）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(obj: Any, key: str) -> str:
    """取字符串字段并去空白；非字符串一律视为空（不炸）。"""
    value = _get(obj, key, "")
    return value.strip() if isinstance(value, str) else ""


def assess_input_sufficiency(req: Any) -> Dict[str, Any]:
    """评估输入充足度，返回报告 dict。

    ``req`` 需具备字段：experiences（含 name/bullets）、education（含 school/major）、
    skills、certifications、phone、email、jd_text、summary。
    """
    experiences = [e for e in (_get(req, "experiences") or []) if _text(e, "name")]
    bullet_count = 0
    bullet_chars = 0
    for e in experiences:
        bullets = [b for b in (_get(e, "bullets") or []) if isinstance(b, str) and b.strip()]
        bullet_count += len(bullets)
        bullet_chars += sum(len(b) for b in bullets)

    education = [e for e in (_get(req, "education") or [])
                 if _text(e, "school") or _text(e, "major")]
    skills = [s for s in (_get(req, "skills") or []) if isinstance(s, str) and s.strip()]
    certs = [c for c in (_get(req, "certifications") or []) if isinstance(c, str) and c.strip()]
    has_contact = bool(_text(req, "phone") or _text(req, "email"))
    has_jd = bool(_text(req, "jd_text"))
    has_summary = bool(_text(req, "summary"))

    score = 0
    gaps: List[str] = []

    # 经历（权重最高，35 分）
    if experiences:
        score += W_EXPERIENCE
        if bullet_count >= MIN_BULLET_COUNT:
            score += W_BULLETS
        else:
            gaps.append("经历要点偏少（不足 3 条），建议补充「做了什么、怎么做的、结果」")
        avg = bullet_chars / max(bullet_count, 1)
        if avg >= MIN_BULLET_AVG_CHARS:
            score += W_BULLET_DETAIL
        else:
            gaps.append("要点描述偏简略（平均每条不足 15 字），建议展开技术细节与成果")
    else:
        gaps.append("缺少项目/实习/工作经历")

    # 教育（15 分）
    if education:
        score += W_EDUCATION
    else:
        gaps.append("缺少教育经历")

    # 技能（15 分）
    if skills:
        score += W_SKILLS
    else:
        gaps.append("缺少技能")

    # 联系方式（10 分）
    if has_contact:
        score += W_CONTACT
    else:
        gaps.append("缺少手机号/邮箱联系方式")

    # 目标 JD（15 分）
    if has_jd:
        score += W_JD
    else:
        gaps.append("未填写目标 JD（无法做关键词对齐）")

    # 总结 / 证书（各 5 分）
    if has_summary:
        score += W_SUMMARY
    if certs:
        score += W_CERTS

    score = max(0, min(100, score))
    if score >= LEVEL_SUFFICIENT:
        level = "sufficient"
    elif score >= LEVEL_MODERATE:
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
