"""简历质量评分器：把「质量铁律」从 prompt 约束落到可计算的代码规则。

评分对象：结构化简历（experiences[].bullets / skills / summary）+ 渲染文本。
输出：总分 + 各维度明细 + 可定位到具体 bullet 的违规清单 + 可直接喂给 LLM 的重写反馈。

设计原则：
1. 纯函数、零 LLM 依赖（只靠正则 + 技能词典 + 词频表），可单测、可复用。
2. 每个违规都能定位到 section（如 experiences[2].bullets[1]），并给一句可执行的改法。
3. feedback_hint 直接作为「质量重试」的 extra_hint 反馈给生成模型，形成闭环。

质量铁律（来自 docs/优秀简历结构分析.md 与 prompts.py）：
- 动词开头：每条 bullet 必须强动词开头，禁止「负责/参与/协助/配合/做了」等弱动词。
- 量化锚点：动词 + 方法 + 量化结果（含基线），禁止空泛。
- STAR 结果导向：每条 bullet 必须落在「改变了什么」（结果词或量化数字）。
- ATS 关键词：技能/经历覆盖目标 JD 关键词（复用 ats_checker + 真实词频表）。
- 字数：700~900 字硬约束；一页 A4。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.agents.tools.ats_checker import check_ats_compatibility
from app.core.skill_keywords import top_keywords
from app.core.text_utils import alias_in_text, normalize_text

# ===== 质量铁律词表（代码化）=====

# 弱动词开头黑名单：bullet 以这些词开头即违规
WEAK_VERB_STARTS = {
    "负责", "参与", "协助", "配合", "做了", "完成", "担任", "进行", "从事",
    "处理", "使用", "维护", "支持", "帮忙", "跟进", "学习", "了解", "熟悉", "掌握",
}

# 强动词开头白名单：bullet 以这些词开头即加分
STRONG_VERB_STARTS = {
    "主导", "搭建", "重构", "优化", "设计", "落地", "构建", "实现", "推动",
    "交付", "孵化", "自研", "引入", "建立", "沉淀", "打磨", "打通", "降低",
    "提升", "支撑", "覆盖", "保障", "缩短", "节省", "开发", "重写", "升级",
    "集成", "封装", "治理", "建设", "打造", "完善", "重构", "牵头", "制定",
}

# 结果导向词：bullet 里出现这些词，说明「落在了结果/价值」上
RESULT_WORDS = {
    "提升", "降低", "缩短", "节省", "覆盖", "支撑", "保障", "增长", "减少",
    "提高", "优化", "达到", "翻倍", "率", "成本", "效率", "耗时",
}

# 空话/套话（buzzwords & filler）：Recruiters 认为「说了等于没说」的通用词。
# 来源：Rezi/Jobscan 明确点名的 buzzwords + 中文简历常见套话。
FILLER_PHRASES = {
    # 英文 buzzwords（Rezi 官方点名）
    "hardworking", "hard working", "team player", "results-driven", "result-driven",
    "detail-oriented", "detail oriented", "proven ability", "self-motivated",
    "self motivated", "go-getter", "dynamic", "proactive", "synergy",
    "fast learner", "quick learner", "excellent communication", "strong work ethic",
    "passionate", "think outside the box", "out of the box",
    # 中文空话套话
    "具备扎实", "熟练掌握", "熟练运用", "熟悉各种", "熟练掌握各种",
    "认真负责", "吃苦耐劳", "积极向上", "团队精神", "良好的沟通",
    "具有较强的", "能够吃苦", "踏实肯干", "有责任心", "学习能力强",
    "抗压能力强", "性格开朗", "善于沟通", "具有团队", "有良好的团队",
    "责任心强", "积极主动", "热爱学习", "善于学习",
}

# summary 开头黑名单：以身份词/空话开头，浪费第一句黄金位置（prompt 已约束，此处代码化验收）
SUMMARY_BAD_STARTS = {
    "应届", "在校", "本科在读", "研究生在读", "学生", "本人", "我是",
    "具备扎实", "熟练掌握", "期待", "热爱", "求职", "应聘", "想找",
}

# 年份（4 位数字，不作为量化成果）
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# 量化锚点正则：数字 + 单位/百分比/倍数/区间
_QUANT_PATTERNS = [
    re.compile(r"\d+\s*[%％]"),                                     # 百分比
    re.compile(r"\d+\s*倍"),                                        # 倍数
    re.compile(r"从\s*\d+\s*(?:到|至|提升到|降到|压缩到|缩短到)\s*\d+"),  # 从 X 到 Y
    re.compile(r"\d+\s*(?:ms|秒|分钟|小时|天|周|月|人|万|千|亿|次|个|篇|条|家|款)"),  # 数量 + 单位
    re.compile(r"(?:qps|dau|tps|uv|pv|gmv|rt)\s*[^\d]{0,4}\d+", re.IGNORECASE),  # 性能指标
    re.compile(r"\d+\s*(?:gb|mb|tb|kb)\b", re.IGNORECASE),          # 存储量
]


def _strip_bullet_prefix(s: str) -> str:
    """去掉 bullet 前导的列表符号（- · • * 序号）与空白。"""
    s = (s or "").strip()
    s = re.sub(r"^[\s\-·•*_>]+", "", s)
    s = re.sub(r"^\d+[.、)）]\s*", "", s)
    return s.strip()


def _start_word(s: str) -> str:
    """取 bullet 开头的前 2 个字符（中文动词多为双字）。"""
    s = _strip_bullet_prefix(s)
    if not s:
        return ""
    # 去掉开头的修饰词「独立 / 从0到1 / 基于XX / 通过XX」不是动词，取前面连续中文字符判断
    m = re.match(r"[\u4e00-\u9fff]{1,2}", s)
    return m.group(0) if m else ""


def _has_quantification(bullet: str) -> bool:
    """是否含量化锚点（排除年份、日期这类非成果数字）。"""
    text = _YEAR_RE.sub("", bullet)
    return any(p.search(text) for p in _QUANT_PATTERNS)


def _has_result(bullet: str) -> bool:
    """是否落在结果/价值上：含量化数字，或含结果导向词。"""
    if _has_quantification(bullet):
        return True
    return any(w in bullet for w in RESULT_WORDS)


def _iter_bullets(structured: Dict[str, Any]):
    """遍历所有 experiences 的 bullets，返回 (定位key, bullet文本)。"""
    for i, exp in enumerate(structured.get("experiences") or []):
        for j, b in enumerate(exp.get("bullets") or []):
            if isinstance(b, str) and b.strip():
                yield f"experiences[{i}].bullets[{j}]", b


@dataclass
class QualityReport:
    total_score: int = 0
    passed: bool = False
    dimensions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    violations: List[Dict[str, Any]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    feedback_hint: str = ""


def _score_verbs(bullets: List[Tuple[str, str]], report: QualityReport) -> float:
    """维度1：动词开头（满分 20）。强动词开头加分，弱动词开头违规。"""
    if not bullets:
        return 0.0
    strong = weak = neutral = 0
    for loc, b in bullets:
        w = _start_word(b)
        if w in WEAK_VERB_STARTS:
            weak += 1
            report.violations.append({
                "section": loc, "dimension": "verb",
                "issue": f"以弱动词「{w}」开头，缺乏主动性与结果感",
                "suggestion": f"改用强动词开头（如 主导/搭建/重构/优化/落地），并补上结果",
            })
        elif w in STRONG_VERB_STARTS:
            strong += 1
        else:
            neutral += 1
    total = len(bullets)
    # 强动词率为主分，弱动词严重扣分，中性不计分不扣分
    score = (strong / total) * 100 - (weak / total) * 40
    return max(0.0, min(100.0, score))


def _score_quant(bullets: List[Tuple[str, str]], report: QualityReport) -> float:
    """维度2：量化锚点（满分 20）。

    ⚠️ 反馈口径必须写死「只能用用户已提供的数字」：
    这一维天然会给模型「缺数字 → 补数字」的压强，如果反馈里只说"补充数字"，
    模型就会编造量化成果（这是简历里最严重的失实）。
    事实真伪由 fidelity.check_bullet_fidelity 兜底，但这里不能主动踩油门。
    """
    if not bullets:
        return 0.0
    q = sum(1 for _, b in bullets if _has_quantification(b))
    score = (q / len(bullets)) * 100
    for loc, b in bullets:
        if not _has_quantification(b):
            report.violations.append({
                "section": loc, "dimension": "quantification",
                "issue": "缺少量化成果，说服力不足",
                "suggestion": (
                    "优先用用户原始信息里**已有**的数字（规模/性能/耗时/覆盖量）；"
                    "用户确实没给数字时，改用不含具体数字的价值表达（如「支撑日常使用」「覆盖核心链路」），"
                    "**严禁自行编造数字**"
                ),
            })
    return score


def _score_result(bullets: List[Tuple[str, str]], report: QualityReport) -> float:
    """维度3：STAR 结果导向（满分 15）。"""
    if not bullets:
        return 0.0
    r = sum(1 for _, b in bullets if _has_result(b))
    score = (r / len(bullets)) * 100
    for loc, b in bullets:
        if not _has_result(b):
            report.violations.append({
                "section": loc, "dimension": "result",
                "issue": "未落在结果/价值上（只写了做了什么，没写改变了什么）",
                "suggestion": "补结果：提升/降低/缩短/覆盖/支撑/保障（数字必须来自用户原始信息，没有就用非数字的价值表达）",
            })
    return score


def _score_filler(summary: str, bullets: List[Tuple[str, str]], report: QualityReport) -> float:
    """维度：空话/套话检测。summary + bullets 出现 buzzwords 即扣分。"""
    hit_count = 0
    s = (summary or "").strip()

    # summary 以身份词/空话开头（浪费第一句黄金位置，prompt 已约束，此处代码化验收）
    for bad in SUMMARY_BAD_STARTS:
        if s.startswith(bad):
            report.violations.append({
                "section": "summary", "dimension": "filler",
                "issue": f"个人总结以「{bad}」开头，是身份词/空话，浪费第一句黄金位置",
                "suggestion": "改为「一句话定位 + 2~3 条带成果的核心亮点」的结果导向开头",
            })
            hit_count += 1
            break

    # summary 正文空话
    for phrase in FILLER_PHRASES:
        if phrase in s:
            report.violations.append({
                "section": "summary", "dimension": "filler",
                "issue": f"个人总结含空话「{phrase}」，说了等于没说",
                "suggestion": f"删除「{phrase}」，改用具体成果替代",
            })
            hit_count += 1

    # bullets 空话
    for loc, b in bullets:
        for phrase in FILLER_PHRASES:
            if phrase in b:
                report.violations.append({
                    "section": loc, "dimension": "filler",
                    "issue": f"含空话「{phrase}」，说了等于没说",
                    "suggestion": f"删除「{phrase}」，改成可验证的成果或具体能力",
                })
                hit_count += 1
                break  # 每条 bullet 只报一次空话

    if hit_count == 0:
        return 100.0
    return max(0.0, 100 - hit_count * 15)


def _check_bullet_count(structured: Dict[str, Any], report: QualityReport) -> None:
    """每条经历至少 3 条要点（Rezi 规则），不足记违规（并入 verb 维度反馈）。"""
    for i, exp in enumerate(structured.get("experiences") or []):
        n = len([b for b in (exp.get("bullets") or []) if isinstance(b, str) and b.strip()])
        if n == 0:
            continue
        if n < 3:
            report.violations.append({
                "section": f"experiences[{i}]", "dimension": "verb",
                "issue": f"该段经历仅 {n} 条要点，少于 3 条，展开不足",
                "suggestion": "补充到至少 3 条要点，每条写足「动词 + 方法 + 量化结果」",
            })


def _score_ats(
    resume_text: str, jd_text: str, direction: str,
    structured: Dict[str, Any], report: QualityReport,
) -> float:
    """维度4：ATS 关键词命中（满分 20）。复用 ats_checker + 方向真实词频覆盖。"""
    ats = check_ats_compatibility(resume_text, jd_text)
    score = float(ats.get("score", 0))

    # 方向词频覆盖：resume_skill_keywords（真实 JD 词频）作为技能覆盖的补充评估
    if direction:
        top = top_keywords(direction, n=12)
        if top:
            skills_text = " ".join(structured.get("skills") or [])
            text_norm = normalize_text(skills_text + " " + resume_text)
            missing = [kw for kw in top if not alias_in_text(kw, text_norm)]
            if missing:
                report.suggestions.append(
                    f"建议补充 {direction} 方向高频关键词：" + "、".join(missing[:6])
                )
                report.violations.append({
                    "section": "skills", "dimension": "ats",
                    "issue": f"{direction} 方向高频关键词覆盖不足（缺 {len(missing)}/{len(top)}）",
                    "suggestion": "补充这些技能关键词：" + "、".join(missing[:8]),
                })
    return score


def _score_length(resume_text: str, report: QualityReport) -> float:
    """维度5：字数（满分 15）。700~900 满分，偏离递减。"""
    n = len((resume_text or "").strip())
    if 700 <= n <= 900:
        return 100.0
    if n < 700:
        report.violations.append({
            "section": "whole", "dimension": "length",
            "issue": f"全文仅 {n} 字，低于 700 字下限，内容不充分",
            "suggestion": "每段经历写满 5 条要点、每条 40+ 字，展开技术细节与结果",
        })
        return max(0.0, 100 - (700 - n) / 700 * 100)
    report.violations.append({
        "section": "whole", "dimension": "length",
        "issue": f"全文 {n} 字，超过 900 字上限，可能超出 A4 一页",
        "suggestion": "精简：删废话、合并同类项、删无关经历",
    })
    return max(0.0, 100 - (n - 900) / 900 * 100)


def _score_one_page(is_too_long: bool, report: QualityReport) -> float:
    """维度6：一页约束（满分 10）。"""
    if is_too_long:
        report.violations.append({
            "section": "whole", "dimension": "one_page",
            "issue": "内容超过 A4 一页",
            "suggestion": "精简到一页：优先删废话/并同类/删无关经历，而非缩小字体",
        })
        return 0.0
    return 100.0


def score_resume_quality(
    structured: Dict[str, Any],
    resume_text: str,
    jd_text: str = "",
    direction: str = "",
    is_too_long: bool = False,
) -> QualityReport:
    """对生成结果做质量评分。

    weights: verb 18 / quant 18 / result 12 / filler 12 / ats 20 / length 10 / one_page 10 = 100
    passed 判定：总分 >= 75 且无「弱动词开头」「字数不足 700」「超一页」硬违规。
    """
    report = QualityReport()
    bullets = list(_iter_bullets(structured))

    v = _score_verbs(bullets, report)
    q = _score_quant(bullets, report)
    r = _score_result(bullets, report)
    f = _score_filler(structured.get("summary", ""), bullets, report)
    _check_bullet_count(structured, report)
    a = _score_ats(resume_text, jd_text, direction, structured, report)
    ln = _score_length(resume_text, report)
    op = _score_one_page(is_too_long, report)

    report.dimensions = {
        "verb":          {"score": round(v),   "weight": 18, "label": "动词开头"},
        "quantification":{"score": round(q),   "weight": 18, "label": "量化锚点"},
        "result":        {"score": round(r),   "weight": 12, "label": "结果导向"},
        "filler":        {"score": round(f),   "weight": 12, "label": "空话套话"},
        "ats":           {"score": round(a),   "weight": 20, "label": "ATS 关键词"},
        "length":        {"score": round(ln),  "weight": 10, "label": "字数"},
        "one_page":      {"score": round(op),  "weight": 10, "label": "一页约束"},
    }
    total = round(
        v * 0.18 + q * 0.18 + r * 0.12 + f * 0.12 + a * 0.20 + ln * 0.10 + op * 0.10
    )
    report.total_score = max(0, min(100, total))

    # 硬违规判定
    hard = []
    weak_bullets = [x for x in report.violations if x["dimension"] == "verb" and "弱动词" in x["issue"]]
    if weak_bullets:
        hard.append(f"{len(weak_bullets)} 条要点弱动词开头")
    if len((resume_text or "").strip()) < 700:
        hard.append("字数不足 700")
    if is_too_long:
        hard.append("超一页")

    report.passed = report.total_score >= 75 and not hard

    # 生成可执行的反馈（喂给重写）
    if report.violations:
        by_dim = {}
        for x in report.violations:
            by_dim.setdefault(x["dimension"], []).append(x)
        parts = [
            "你上一版简历存在以下质量问题，请针对性重写：",
            "【事实红线（优先级高于下面所有质量问题）】任何数字（百分比/倍数/次数/规模/客户数/耗时）"
            "都必须能在用户原始信息里找到出处；找不到出处的一律不许写，"
            "宁可改成不含具体数字的价值表达，也绝不允许编造。",
        ]
        for dim, items in by_dim.items():
            label = report.dimensions.get(dim, {}).get("label", dim)
            parts.append(f"【{label}】" + "；".join(
                f"{it['section']}：{it['suggestion']}" for it in items[:5]
            ))
        report.feedback_hint = "\n".join(parts)
        # 合并各维度阶段 append 的建议 + 违规建议（去重保序）
        extra = [x["suggestion"] for x in report.violations[:10]]
        report.suggestions = list(dict.fromkeys(report.suggestions + extra))

    return report


def report_to_dict(report: QualityReport) -> Dict[str, Any]:
    """把评分报告转成可序列化 dict（存库 / 返回前端）。"""
    return {
        "total_score": report.total_score,
        "passed": report.passed,
        "dimensions": report.dimensions,
        "violations": report.violations,
        "suggestions": report.suggestions,
    }
