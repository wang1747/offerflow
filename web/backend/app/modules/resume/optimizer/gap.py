"""简历相对 JD 的差距分析（v2：逐条判定 matched / partial / missing）。

确定性、不依赖 LLM（语义层用纯标准库的 app.core.semantic），用于：
1. 告诉 LLM 哪些 JD 关键词已经命中、哪些缺失（不要硬塞）、哪些相关但需强化；
2. 给前端展示「本次优化针对哪些关键词」。

与 v1 的区别：不再把「措辞不同但意思到位」的需求一刀切成 missing，
而是三级判定——精确命中 → matched，语义近似 → partial，真缺口 → missing。
"""

import re
from typing import Dict, List, Optional, Set, Tuple

from app.core import semantic
from app.core.text_utils import (
    SKILL_ALIASES, alias_in_text, extract_skills, normalize_text,
)

from .contracts import GapAnalysis, JDRequirement


def _canonical_skill(term: str) -> Optional[str]:
    """如果 term 是某个技能名或其别名，返回规范技能名。"""
    normalized = normalize_text(term)
    for skill, aliases in SKILL_ALIASES.items():
        if normalized == normalize_text(skill):
            return skill
        for alias in aliases:
            if normalized == normalize_text(alias):
                return skill
    return None


def _aliases_of(term: str) -> List[str]:
    """返回 term 的所有别名（含自身），用于在简历文本里查。"""
    skill = _canonical_skill(term)
    result = [normalize_text(term)]
    if skill:
        result.append(normalize_text(skill))
        result.extend(normalize_text(a) for a in SKILL_ALIASES.get(skill, []))
    extra: List[str] = []
    for r in list(result):
        if re.fullmatch(r"[a-z0-9+#./ -]+", r):
            extra.append(r.replace("-", "").replace(" ", ""))
    return [x for x in result + extra if x]


# 学历等级（数字越大越高），用于「本科及以上」这类门槛的确定性判定
_DEGREE_RANK: Dict[str, int] = {
    "大专": 1, "专科": 1, "高职": 1, "中专": 1,
    "本科": 2, "学士": 2, "在读本科": 2,
    "硕士": 3, "研究生": 3, "mba": 3,
    "博士": 4, "博士后": 4,
}


def _degree_rank(text: str) -> int:
    """文本里的最高学历等级（没有任何学历词时返回 0）。"""
    low = (text or "").lower()
    best = 0
    for name, rank in _DEGREE_RANK.items():
        if name in low and rank > best:
            best = rank
    return best


# 中文「非技能型」需求词 → 简历侧可接受的证据写法。
CN_REQ_EVIDENCE: Dict[str, Tuple[str, ...]] = {
    "项目经验": ("项目",), "项目经历": ("项目",), "项目背景": ("项目",),
    "实习经验": ("实习",), "实习经历": ("实习",),
    "工作经验": ("工作", "实习"), "工程经验": ("项目", "实习", "工程"),
    "英文": ("cet 4", "cet 6", "cet4", "cet6", "英语", "雅思", "托福", "六级", "四级"),
    "英语": ("cet 4", "cet 6", "cet4", "cet6", "雅思", "托福", "六级", "四级"),
    "团队协作": ("团队", "协作", "小组"), "团队合作": ("团队", "合作", "小组"),
    "沟通能力": ("沟通", "对接", "协调", "宣讲"),
    "学习能力": ("学习", "自学"),
    "文档": ("文档",), "技术文档": ("文档", "规范", "手册"),
    "案例": ("案例", "项目"),
}


def _cn_req_hit(requirement: str, resume_norm: str) -> Tuple[int, int]:
    """统计需求里的中文非技能词有多少能在简历里找到落点。"""
    hits = total = 0
    for term, evidences in CN_REQ_EVIDENCE.items():
        if term in requirement:
            total += 1
            if any(ev in resume_norm for ev in evidences):
                hits += 1
    return hits, total


def _dedup(items) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for item in items or []:
        text = (item or "").strip() if isinstance(item, str) else ""
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


# JD 里的小标题 / 板块标签，不是需求本身
_JD_HEADING_RE = re.compile(
    r"^(?:岗位职责|工作职责|职位职责|职责描述|职位描述|岗位描述|工作内容|主要职责|"
    r"任职要求|任职资格|任职条件|岗位要求|资格要求|职位要求|能力要求|我们要求|"
    r"加分项|优先条件|优先项|我们希望|我们希望你|你将负责|你会做什么|你将获得|"
    r"薪酬福利|福利待遇|薪资待遇|关于我们|公司介绍|团队介绍|联系方式|简历投递)"
    r"[：:]?$"
)
_JD_PURE_NUMBER_RE = re.compile(r"^[（(]?\d{1,2}[)）.、\s]*[）)]?$")


def _is_jd_heading(text: str) -> bool:
    """判断一行是不是 JD 的小标题/标签（而不是一条需求）。"""
    raw = (text or "").strip()
    if not raw:
        return True
    if _JD_HEADING_RE.match(raw):
        return True
    if _JD_PURE_NUMBER_RE.match(raw):
        return True
    stripped = raw.rstrip("：:").strip()
    if raw.endswith(("：", ":")) and len(stripped) <= 10 and not re.search(r"[a-z0-9]", raw):
        return True
    return False


def _jd_lines(raw_text: str, limit: int = 24) -> List[str]:
    """JD 原文按行切成候选需求（结构化字段缺失时的兜底）。"""
    lines: List[str] = []
    for line in (raw_text or "").splitlines():
        text = line.strip().strip("-•·*–—> \t")
        if 4 <= len(text) <= 120 and not _is_jd_heading(text):
            lines.append(text)
    return _dedup(lines)[:limit]


def requirements_of(jd: JDRequirement) -> List[Dict[str, object]]:
    """把 JD 拆成带权重的需求列表。

    权重口径：must_have 1.0、tech_stack 0.8、nice_to_have 0.5、
    从原文兜底抽出的行 0.9。原文兜底只在结构化字段过于稀疏时启用。
    """
    reqs: List[Dict[str, object]] = []
    seen: Set[str] = set()

    def _push(text: str, weight: float, kind: str) -> None:
        if not text or text in seen:
            return
        seen.add(text)
        reqs.append({"text": text, "weight": weight, "kind": kind})

    for text in _dedup(jd.must_have):
        _push(text, 1.0, "must")
    for text in _dedup(jd.tech_stack):
        _push(text, 0.8, "tech")
    for text in _dedup(jd.nice_to_have):
        _push(text, 0.5, "nice")

    must_count = sum(1 for r in reqs if r["kind"] == "must")
    if must_count < 2:
        for line in _jd_lines(jd.raw_text):
            _push(line, 0.9, "raw")
    return reqs


def _evidence_for_skills(skills: Set[str], chunks: List[str]) -> Tuple[str, List[str]]:
    """在片段里找一条能支撑这些技能的原文。"""
    best_chunk, best_terms = "", []
    for chunk in chunks:
        hit = [s for s in skills if alias_in_text(normalize_text(s), normalize_text(chunk))]
        if len(hit) > len(best_terms):
            best_chunk, best_terms = chunk, sorted(hit)
    return best_chunk, best_terms


def _classify(requirement: str, resume_norm: str, resume_skills: Set[str],
              chunks: List[str]) -> Tuple[str, float, str, List[str]]:
    """判定单条需求：返回 (status, score, evidence, missing_skills)。"""
    # 1) 整串即别名（短需求，如「Python」「MySQL」）——按词边界精确命中
    for alias in _aliases_of(requirement):
        if alias_in_text(alias, resume_norm):
            return "matched", 1.0, "", []
    canonical = _canonical_skill(requirement)
    if canonical and canonical in resume_skills:
        return "matched", 1.0, "", []

    # 2) 需求句里提到的技能词覆盖率
    req_skills = extract_skills(requirement)
    if req_skills:
        have = req_skills & resume_skills
        ratio = len(have) / float(len(req_skills))
        evidence, terms = _evidence_for_skills(have, chunks) if have else ("", [])
        if ratio >= 1.0:
            return "matched", 1.0, evidence, []
        if ratio >= 0.5:
            return "partial", round(ratio, 4), evidence, sorted(req_skills - resume_skills)

    # 2.5) 学历门槛
    req_degree = _degree_rank(requirement)
    if req_degree and _degree_rank(resume_norm) >= req_degree:
        return "matched", 1.0, "", []

    # 2.6) 中文非技能型需求词
    cn_hits, cn_total = _cn_req_hit(requirement, resume_norm)
    if cn_total and cn_hits == cn_total:
        return "matched", 1.0, "", []
    if cn_total and cn_hits:
        return "partial", round(cn_hits / float(cn_total), 4), "", []

    # 3) 语义近似：措辞不同但确实相关 → partial，并给出证据句
    score, chunk, _terms = semantic.best_evidence(
        requirement, chunks, threshold=semantic.PARTIAL_THRESHOLD)
    if score > 0:
        return "partial", score, chunk, []

    # 4) 真缺口
    return "missing", 0.0, "", sorted(req_skills)


def analyze_gap(resume_text: str, jd: JDRequirement) -> GapAnalysis:
    """计算简历相对 JD 的 matched / partial / missing（逐条判定）。"""
    resume_norm = normalize_text(resume_text)
    resume_skills = extract_skills(resume_text)
    chunks = _dedup([
        ln.strip().lstrip("-•·*–— ").strip()
        for ln in (resume_text or "").splitlines() if len(ln.strip()) >= 4
    ])

    matched: List[str] = []
    partial: List[str] = []
    missing: List[str] = []

    for req in requirements_of(jd):
        status, _score, _evidence, _miss = _classify(
            req["text"], resume_norm, resume_skills, chunks)
        if status == "matched":
            matched.append(req["text"])
        elif status == "partial":
            partial.append(req["text"])
        else:
            missing.append(req["text"])

    return GapAnalysis(matched=matched, missing=missing, partial=partial)
