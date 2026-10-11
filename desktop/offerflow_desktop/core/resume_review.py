"""简历内容质检规则引擎（对标成熟简历产品的撰写规范，确定性、不调 LLM）。

设计目标：把「什么样的句子算好句子」变成可执行、可打分的规则，
补上 OfferFlow 原先只有 ATS 关键词覆盖、没有内容质量反馈的缺口。

规则来源见 docs/简历编写能力对标报告.md 第六节「目标规格」。

对外入口：
    review_resume(structured=..., text=..., jd_text=...)  -> dict
    review_structured(structured, jd_text="")             -> dict
    review_text(text, jd_text="")                         -> dict
返回：
    {
      "score": 0~100, "level": "优秀/良好/及格/偏弱",
      "issues": [{"severity","category","location","message","fix"}],
      "stats": {...}, "highlights": [...],
    }
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from . import continuity, expression_bank

# ============ 词表（自成熟产品规范抽取） ============

WEAK_VERBS: Tuple[str, ...] = (
    "负责", "参与", "协助", "帮助", "配合", "完成", "跟进",
    "支持", "从事", "听从", "搞好", "帮忙",
)

STRONG_VERB_MAP: Dict[str, str] = {
    "负责": "主导", "参与": "主导", "协助": "协同推进", "帮助": "支撑",
    "配合": "协同", "完成": "交付", "跟进": "推进", "支持": "支撑",
    "从事": "深耕", "听从": "承接", "搞好": "落地", "帮忙": "支撑",
}

STRONG_VERBS: Tuple[str, ...] = (
    "主导", "搭建", "重构", "优化", "设计", "落地", "推动", "沉淀", "拆解",
    "抽象", "迁移", "引入", "自动化", "建模", "调优", "压测", "上线", "协同",
    "攻坚", "策划", "实现", "牵头", "制定", "建立", "输出", "整合", "迭代",
    "复盘", "定位", "验证", "打通", "补齐", "降低", "提升", "缩短", "复用",
    "开发", "撰写", "梳理", "独立",
)

CLICHES: Tuple[str, ...] = (
    "吃苦耐劳", "性格开朗", "有责任心", "责任心强", "团队合作精神", "团队合作",
    "抗压能力强", "抗压能力", "沟通能力强", "学习能力强", "积极主动", "认真负责",
    "做事踏实", "能吃苦", "热爱学习", "善于沟通", "具有良好的", "具有较强的",
    "有较强的", "良好的沟通能力", "较强的学习能力", "工作认真", "踏实肯干",
    "活泼开朗", "服从安排",
)

VAGUE_WORDS: Tuple[str, ...] = (
    "很多", "一些", "大量", "若干", "比较好", "不错", "有一定的",
    "各种各样", "等等", "比较丰富", "各方面",
)

# 量化单位（命中任意一项视为该条要点已量化）
QUANT_RE = re.compile(
    # ① 阿拉伯数字 + 可选「余/多/左右/上下/以上」+ 单位
    #    中文简历常写「100 余份」「30 多家」——数字和量词之间夹着「余/多」，
    #    早先的正则要求两者紧邻，导致这些真量化被判成 0 量化。
    r"\d+(?:\.\d+)?\s*(?:余|多|左右|上下|以上|\+|＋)?\s*"
    r"(?:%|％|个百分点|万|亿|千|百|倍|人|次|个|项|条|篇|台|家|份|分|例|元|天"
    r"|周|月|年|小时|分钟|秒|ms|MS|K|k|w|W|GB|MB|TB|Gb|Mbps|Gbps|QPS|TPS|UV|PV)"
    # ② 中文数字 + 量词（十余个、百余份、三十多家、上百人、两百多例）
    #    这里**故意不含「分」**：「十分熟悉」是程度副词而非量化。
    r"|[一二三四五六七八九十百千万两]+(?:余|多)?\s*"
    r"(?:%|％|个百分点|倍|人|次|个|项|条|篇|台|家|份|例|元|天|周|月|年|小时|分钟|秒)"
    # ③ 「数字 + 万字」：报告/稿件的产出体量（十字路口的「十字」不会命中）
    r"|[一二三四五六七八九十百千万两]+万字"
    # ④ 小数指标：0.857、1.0、99.5 —— 召回率/准确率/得分这类指标常写成小数，
    #    且后面不带单位。前后加边界，避免把「2026.03」这种年月误判成指标。
    r"|(?<![\d.])\d{1,2}\.\d+(?![\d.])"
    # ⑤ 前后对比式量化：从 0.857 提升到 1.0
    r"|从\s*[\d.]+\s*(?:提升|提高|降低|降至|增长|下降|优化|压缩)到?\s*[\d.]+"
)

BULLET_HARD_MAX = 72        # 超过视为过长
BULLET_HARD_MIN = 20        # 低于视为过短
BULLET_TARGET = (25, 60)    # 规范目标区间（成熟产品标准，质检按此分档扣分）
EXP_BULLET_RANGE = (3, 5)   # 每段要点条数（与提示词「3~5 条」保持一致）
FULL_CHAR_RANGE = (700, 1100)
QUANT_TARGET = 0.60         # 量化率目标
KEYWORD_MAX_REPEAT = 5      # 单个 JD 关键词全篇出现次数上限（超过视为堆砌）
RELEVANCE_GAP = 3           # 排序校验：靠后经历命中的 JD 词比首段多几个才提示

# 方法/技术词（用于 C1「四要素」中「方法/技术」的确定性检测）
METHOD_MARKERS = re.compile(
    r"通过|借助|基于|采用|使用|利用|引入|运用|结合|依托|围绕|针对|面向"
    r"|算法|模型|索引|缓存|队列|中间件|架构|接口|数据库|脚本|自动化|流水线|工具链"
    r"|框架|组件|模块|服务|正则|多线程|异步|事务|查询|测试|用例|规范|流程|方案"
    r"|机制|指标|模板|复盘|拆解|对账|埋点|分片|降级|限流|聚合|抽取|解析|调度|部署"
    r"|文档|需求|调研|报告|策略|标准|体系|清单|评审|培训|排期|原型|问卷|数据|可视化"
    r"|Docker|K8s|Kubernetes|Redis|MySQL|PostgreSQL|MongoDB|Linux|Git|Nginx|Jenkins"
    r"|Spring|FastAPI|Django|Flask|React|Vue|PyTorch|TensorFlow|Transformer|RAG|LLM"
    r"|SQL|NoSQL|Python|Java|Golang|TypeScript|JavaScript|C\+\+|API|SDK|CI/CD",
    re.I,
)

# 成果词（用于 C1/C10 判断「是否体现了结果」，是量化之外的可辩护表述）
RESULT_MARKERS = re.compile(
    r"提升|提高|降低|减少|缩短|节省|增长|翻倍|达成|超出|排名|获奖|上线|交付|落地"
    r"|覆盖|支撑|承接|通过率|成功率|准确率|召回率|转化率|独立|从 ?0 到 ?1|从零到一",
)

# 身份/方向词（用于 C10 判断个人总结是否有「身份定位」）
IDENTITY_MARKERS = re.compile(
    r"工程师|开发|研发|算法|前端|后端|全栈|测试|运维|数据|产品|设计|运营|分析"
    r"|应届|在读|本科|硕士|研究生|博士|专业|方向|实习",
)

# ============ 分节识别 ============

SECTION_ALIASES: Dict[str, str] = {
    "求职意向": "position", "意向岗位": "position", "目标岗位": "position",
    "个人总结": "summary", "自我评价": "summary", "个人简介": "summary",
    "教育经历": "education", "教育背景": "education",
    "项目经历": "experience", "项目经验": "experience",
    "实习经历": "experience", "工作经历": "experience",
    "校园经历": "experience", "实践经历": "experience",
    "专业技能": "skills", "技能特长": "skills", "技能证书": "skills", "技能": "skills",
    "获奖经历": "honors", "荣誉奖项": "honors", "荣誉": "honors", "奖项": "honors",
    "证书": "certifications", "资格证书": "certifications",
}

SECTION_TITLES: Dict[str, str] = {
    "position": "求职意向", "summary": "个人总结", "education": "教育经历",
    "experience": "经历", "skills": "技能", "honors": "荣誉", "certifications": "证书",
}

_SECTION_KEYS: List[str] = sorted(SECTION_ALIASES, key=len, reverse=True)

_BULLET_PREFIXES = ("- ", "• ", "· ", "* ", "– ", "— ", "▪ ", "● ")

_PERIOD_RE = re.compile(
    r"((?:19|20)\d{2}(?:[.\-/年]\d{1,2})?(?:[.\-/年]\d{1,2})?)"
    r"\s*(?:[~～–—\-]{1,2}|到|至|→)\s*"
    r"((?:19|20)\d{2}(?:[.\-/年]\d{1,2})?(?:[.\-/年]\d{1,2})?|至今|今|现在|now)"
)

_NUM_YEAR_RE = re.compile(r"(?:19|20)\d{2}(?:[.\-/年]\d{1,2})?")

_TYPE_GROUP = {"project": "项目经历", "internship": "实习经历", "work": "工作经历"}


# ============ 文本解析 ============


def _is_bullet(line: str) -> bool:
    return line.startswith(_BULLET_PREFIXES)


def _strip_bullet(line: str) -> str:
    for prefix in _BULLET_PREFIXES:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return line.strip()


def _match_section(line: str) -> Optional[str]:
    """判断一行是否是小节标题，返回归一化的小节类型。"""
    text = line.strip().strip("【】[]#*· ").rstrip("：:").strip()
    if not text or len(text) > 12:
        return None
    for key in _SECTION_KEYS:
        if text == key:
            return SECTION_ALIASES[key]
    for key in _SECTION_KEYS:
        if text.startswith(key) and len(text) <= len(key) + 4:
            return SECTION_ALIASES[key]
    return None


def normalize_period(start: str, end: str) -> str:
    """把时间写法统一成 2025.03 – 2026.06 / 2025.03 – 至今。"""
    def _clean(value: str) -> str:
        text = (value or "").strip()
        text = text.replace("年", ".").replace("月", "").replace("日", "")
        # 2023-09 / 2025/7 → 2023.09 / 2025.7（要求月份后面不跟数字，避免误伤 2025-2026）
        text = re.sub(r"(\d{4})\s*[.\-/]\s*(\d{1,2})(?!\d)", r"\1.\2", text)
        text = re.sub(r"^[.\s]+|[.\s]+$", "", text)
        parts = [p for p in text.split(".") if p]
        if len(parts) >= 2 and len(parts[1]) == 1:
            parts[1] = "0" + parts[1]
        return ".".join(parts[:2])

    left = _clean(start)
    right = (end or "").strip()
    if not right:
        return left
    if right in ("今", "现在", "now"):
        right = "至今"
    else:
        right = _clean(right)
    return f"{left} – {right}".strip(" –")


def split_header(line: str) -> Tuple[str, str]:
    """把「名称（角色，2025.03 ~ 2026.06）」拆成 ("名称（角色）", "2025.03 – 2026.06")。"""
    match = _PERIOD_RE.search(line)
    period = ""
    left = line
    if match:
        period = normalize_period(match.group(1), match.group(2))
        left = line[:match.start()] + line[match.end():]
    elif _NUM_YEAR_RE.search(line):
        # 只有单个时间（如「2027.06」）
        single = _NUM_YEAR_RE.search(line)
        period = normalize_period(single.group(0), "")
        period = period.split(" – ")[0]
        left = line[:single.start()] + line[single.end():]
    left = re.sub(r"[（(]\s*[，,、｜|]?\s*[)）]", "", left)
    left = re.sub(r"[，,、]\s*([)）])", r"\1", left)
    left = re.sub(r"[，,、｜|]\s*$", "", left)
    left = re.sub(r"\s{2,}", " ", left)
    return left.strip(" 、，,｜|"), period


def _exp_group(line: str) -> str:
    """把「项目经历 / 实习经历 / 工作经历」归类，供排版分组使用。"""
    for key, label in (("项目", "项目经历"), ("实习", "实习经历"),
                       ("工作", "工作经历"), ("校园", "校园经历"), ("实践", "实践经历")):
        if key in line:
            return label
    return "经历"


def parse_resume_text(text: str) -> Dict[str, Any]:
    """把简历纯文本解析成结构化块，供质检与排版共用。"""
    lines = [line.rstrip() for line in (text or "").splitlines()]
    doc: Dict[str, Any] = {
        "name": "", "contact_line": "", "position": "", "summary": "",
        "education": [], "experiences": [], "skills": [], "skill_groups": {},
        "certifications": [],
        "honors": [], "extra": [], "section_order": [],
    }

    # 1. 姓名 / 联系方式（前两行非空内容）
    head_idx = [i for i, l in enumerate(lines) if l.strip()][:2]
    if head_idx:
        doc["name"] = lines[head_idx[0]].strip()
    if len(head_idx) > 1:
        candidate = lines[head_idx[1]].strip()
        if re.search(r"[\w.+-]+@[\w-]+|1[3-9]\d{9}", candidate):
            doc["contact_line"] = candidate

    current: Optional[str] = None
    current_group = "经历"
    current_exp: Optional[Dict[str, Any]] = None
    extra_holder: Optional[Dict[str, Any]] = None
    # 行号锚点：键统一叫 "line"，值是**原文 1-based 行号**（与编辑器行号一致）。
    # 多记这一份，是为了让质检结论能"钉"回原文那一行 —— 目前只说
    # "某段经历要点过长"，用户还得自己在整篇里找是哪一条。
    # 只记物理行号，不记"第几条要点"：物理行号才能被编辑器直接定位。
    doc["skill_lines"] = []
    doc["cert_lines"] = []
    doc["summary_line"] = None

    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        lineno = idx + 1
        if line == doc["name"] or line == doc["contact_line"]:
            continue

        section = _match_section(line)
        if section:
            current = section
            current_group = _exp_group(line) if section == "experience" else "经历"
            current_exp = None
            extra_holder = None
            if section not in doc["section_order"]:
                doc["section_order"].append(section)
            continue

        if current is None:
            # 分节之前的内容：当成 summary 之外的散行，忽略
            continue

        if current == "position":
            if not doc["position"]:
                doc["position"] = line
        elif current == "summary":
            if doc["summary_line"] is None:
                doc["summary_line"] = lineno
            doc["summary"] = (doc["summary"] + " " + line).strip()
        elif current == "education":
            left, period = split_header(line)
            doc["education"].append({"left": left or line, "period": period,
                                     "details": [], "line": lineno})
        elif current == "experience":
            if _is_bullet(line):
                if current_exp is None:
                    current_exp = {"left": "", "period": "", "bullets": [],
                                   "group": current_group, "line": lineno,
                                   "bullet_lines": []}
                    doc["experiences"].append(current_exp)
                current_exp["bullets"].append(_strip_bullet(line))
                current_exp.setdefault("bullet_lines", []).append(lineno)
            else:
                left, period = split_header(line)
                current_exp = {"left": left or line, "period": period, "bullets": [],
                               "group": current_group, "line": lineno,
                               "bullet_lines": []}
                doc["experiences"].append(current_exp)
        elif current == "skills":
            items = _split_items(line)
            doc["skills"].extend(items)
            doc["skill_lines"].extend([lineno] * len(items))
        elif current == "certifications":
            items = _split_items(line)
            doc["certifications"].extend(items)
            doc["cert_lines"].extend([lineno] * len(items))
        elif current == "honors":
            doc["honors"].append(_strip_bullet(line))
        else:
            if extra_holder is None or extra_holder.get("type") != current:
                extra_holder = {"type": current, "title": SECTION_TITLES.get(current, "其他"), "lines": []}
                doc["extra"].append(extra_holder)
            extra_holder["lines"].append(_strip_bullet(line))

    return doc


def _split_items(line: str) -> List[str]:
    text = _strip_bullet(line)
    text = re.sub(r"^[^：:]{2,8}[：:]", "", text)  # 去掉「语言：」这类前缀
    parts = re.split(r"[、,，;；/|]", text)
    return [p.strip() for p in parts if p.strip()]


def from_structured(structured: Dict[str, Any]) -> Dict[str, Any]:
    """把结构化简历（LLM 输出）转成解析块，字段与 parse_resume_text 对齐。"""
    structured = structured if isinstance(structured, dict) else {}
    contact = structured.get("contact") if isinstance(structured.get("contact"), dict) else {}
    contact_line = " | ".join(
        str(contact.get(k)).strip() for k in ("phone", "email", "github")
        if str(contact.get(k) or "").strip()
    )

    education = []
    for item in structured.get("education") or []:
        if not isinstance(item, dict):
            continue
        main = " ".join(str(item.get(k) or "").strip() for k in ("school", "major", "degree"))
        period = ""
        if str(item.get("start") or "").strip() or str(item.get("end") or "").strip():
            period = normalize_period(str(item.get("start") or ""), str(item.get("end") or ""))
        education.append({
            "left": re.sub(r"\s{2,}", " ", main).strip(),
            "period": period,
            "details": [str(item.get("detail") or "").strip()] if str(item.get("detail") or "").strip() else [],
        })

    experiences = []
    for item in structured.get("experiences") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        role = str(item.get("role") or "").strip()
        left = f"{name}（{role}）" if name and role else (name or role)
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        period = normalize_period(start, end or ("至今" if start else "")) if (start or end) else ""
        bullets = [str(b).strip() for b in (item.get("bullets") or []) if str(b).strip()]
        experiences.append({
            "left": left, "period": period, "bullets": bullets,
            "group": _TYPE_GROUP.get(str(item.get("type") or "project"), "项目经历"),
        })

    skills: List[str] = []
    skill_groups: Dict[str, List[str]] = {}
    groups = structured.get("skill_groups")
    if isinstance(groups, dict) and groups:
        for group_name, values in groups.items():
            if isinstance(values, list):
                items = [str(v).strip() for v in values if str(v).strip()]
                if items:
                    skill_groups[str(group_name).strip() or "技能"] = items
                    skills.extend(items)
    if not skills:
        skills = [str(s).strip() for s in (structured.get("skills") or []) if str(s).strip()]

    return {
        "name": str(structured.get("name") or "").strip(),
        "contact_line": contact_line,
        "position": str(structured.get("position") or "").strip(),
        "summary": str(structured.get("summary") or "").strip(),
        "education": education,
        "experiences": experiences,
        "skills": skills,
        "skill_groups": skill_groups,
        "certifications": [str(c).strip() for c in (structured.get("certifications") or []) if str(c).strip()],
        "honors": [],
        "extra": [],
        "section_order": [],
    }


# ============ 规则引擎 ============


class _Report:
    def __init__(self, doc: Dict[str, Any], raw_text: str, jd_text: str = ""):
        self.doc = doc
        self.raw_text = raw_text or ""
        self.jd_text = jd_text or ""
        self.issues: List[Dict[str, Any]] = []
        self.highlights: List[str] = []
        self._deduct: Dict[str, float] = {}

    # --- 扣分（按类别设上限，避免同一类问题刷爆分数） ---
    def add(self, category: str, severity: str, location: str, message: str,
            fix: str = "", cost: float = 0.0, cap: float = 10.0,
            line: Optional[int] = None) -> None:
        """记一条问题。

        ``line`` 是原文 1-based 行号（没有原文可指时留 None）。**不填也合法**：
        很多结论天然是整篇级的（缺邮箱、时间线倒挂），硬凑一个行号反而误导。
        """
        used = self._deduct.get(category, 0.0)
        allowed = max(0.0, min(cost, cap - used))
        self._deduct[category] = used + allowed
        issue: Dict[str, Any] = {
            "severity": severity, "category": category,
            "location": location, "message": message, "fix": fix,
            "line": line,
        }
        self.issues.append(issue)

    @property
    def score(self) -> int:
        total = sum(self._deduct.values())
        return int(max(0, min(100, round(100 - total))))


def line_text(raw_text: str, line: Optional[int], *, limit: int = 60) -> str:
    """取原文第 ``line`` 行（1-based）的内容，越界或没行号返回空串。

    界面上"钉到行"靠的就是它：显示第几行 + 那一行原文，
    比只给一句"要点过长"可操作得多。
    """
    try:
        n = int(line)                                     # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    rows = (raw_text or "").splitlines()
    if n > len(rows):
        return ""
    text = rows[n - 1].strip()
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    return text


def _bullets_of(doc: Dict[str, Any]
                ) -> List[Tuple[str, str, Optional[int]]]:
    """返回 [(所属经历, 要点文本, 原文行号)]。

    行号只在"从纯文本解析"这条路上有值；"从结构化数据来"的那条路没有
    原文可指，就是 None —— 界面上不显示行号，而不是编一个凑数。
    """
    pairs: List[Tuple[str, str, Optional[int]]] = []
    for idx, exp in enumerate(doc.get("experiences") or [], start=1):
        label = exp.get("left") or f"第 {idx} 段经历"
        lines = list(exp.get("bullet_lines") or [])
        for pos, bullet in enumerate(exp.get("bullets") or []):
            ln = lines[pos] if pos < len(lines) else None
            pairs.append((label, bullet, ln))
    return pairs


def _has_quant(text: str) -> bool:
    return bool(QUANT_RE.search(text or ""))


def _starts_weak(text: str) -> Optional[str]:
    for verb in WEAK_VERBS:
        if text.startswith(verb):
            return verb
    return None


def _suggest_rewrite(text: str, verb: str) -> str:
    """把弱动词开头换成更强的写法（由表达库驱动，带领域感知）。"""
    strong = expression_bank.upgrade_verb(verb) or STRONG_VERB_MAP.get(verb, "主导")
    return strong + text[len(verb):]


def _quant_tip(text: str) -> str:
    """缺量化时，按句子内容提示该补哪个维度的指标。"""
    if _has_quant(text):
        return ""
    hints = expression_bank.metric_hints(text, limit=2)
    if not hints:
        return ""
    return "；建议补的量化维度：" + "；".join(hints)


def _project_text(doc: Dict[str, Any]) -> str:
    """把解析块拼回纯文本，用于字数统计与标点/JD 检查（结构化路径下 raw 为空）。"""
    parts: List[str] = [
        doc.get("name") or "", doc.get("contact_line") or "",
        doc.get("position") or "", doc.get("summary") or "",
    ]
    for edu in doc.get("education") or []:
        parts.extend([edu.get("left") or "", edu.get("period") or ""])
        parts.extend(edu.get("details") or [])
    for exp in doc.get("experiences") or []:
        parts.extend([exp.get("left") or "", exp.get("period") or ""])
        parts.extend(exp.get("bullets") or [])
    parts.extend(doc.get("skills") or [])
    parts.extend(doc.get("certifications") or [])
    parts.extend(doc.get("honors") or [])
    for extra in doc.get("extra") or []:
        parts.extend(extra.get("lines") or [])
    return "\n".join(p for p in parts if p)


def review_resume(structured: Optional[Dict[str, Any]] = None,
                  text: str = "", jd_text: str = "") -> Dict[str, Any]:
    """统一入口：优先用结构化数据，否则解析纯文本。"""
    raw = text or ""
    if isinstance(structured, dict) and structured:
        doc = from_structured(structured)
    else:
        doc = parse_resume_text(text)
    # 结构化路径下没有原文，用解析块拼回文本，保证字数/标点/JD 检查可用
    if not raw.strip():
        raw = _project_text(doc)

    rep = _Report(doc, raw, jd_text)
    bullets = _bullets_of(doc)
    stats: Dict[str, Any] = {
        "char_count": len(re.sub(r"\s", "", raw)) if raw else 0,
        "bullet_count": len(bullets),
        "exp_count": len(doc.get("experiences") or []),
        "weak_verb_count": 0,
        "quantified_count": 0,
        "quantified_ratio": 0.0,
        "avg_bullet_len": 0,
        "cliche_hits": [],
        "vague_hits": [],
        "skill_count": len(doc.get("skills") or []),
        "has_summary": bool(doc.get("summary")),
    }

    # --- 1. 联系方式 ---
    contact = doc.get("contact_line") or ""
    if not re.search(r"[\w.+-]+@[\w-]+\.[\w]+", contact):
        rep.add("联系信息", "high", "页眉", "缺少邮箱联系方式",
                "在联系方式行补上邮箱，ATS 与 HR 都会优先找它", cost=12, cap=20)
    if not re.search(r"1[3-9]\d{9}|\+?\d[\d\s-]{7,}\d", contact):
        rep.add("联系信息", "high", "页眉", "缺少手机号",
                "补上手机号（建议 3-4-4 分段，如 138-0000-0000）", cost=8, cap=20)

    # --- 2. 分节完整性 ---
    if not doc.get("experiences"):
        rep.add("结构", "high", "整体", "未识别到项目/实习/工作经历",
                "至少写一段与岗位相关的经历，这是简历的核心部分", cost=15, cap=20)
    if not doc.get("education"):
        rep.add("结构", "medium", "整体", "未识别到教育经历",
                "应届生简历必须有教育经历（学校/专业/学历/起止时间）", cost=6, cap=20)
    if not doc.get("skills"):
        rep.add("结构", "medium", "技能", "未识别到技能模块",
                "按「语言 / 框架与库 / 工具与平台」分组列 8~20 项技能", cost=5, cap=20)
    if not stats["has_summary"]:
        rep.add("结构", "low", "个人总结", "缺少个人总结",
                "用「身份定位 + 2~3 项核心能力 + 1 个代表成果」写 3~4 句", cost=5, cap=20)
    else:
        rep.highlights.append("有个人总结，HR 6 秒扫读时能第一眼看到你的定位")
        # 个人总结/自我评价是套话最高发的地方，必须单独扫一遍
        summary_text = doc.get("summary") or ""
        s_line = doc.get("summary_line")           # 个人总结的首行，用于钉回原文
        for cliche in CLICHES:
            if cliche in summary_text:
                stats["cliche_hits"].append(cliche)
                rep.add("套话", "high", "个人总结",
                        f"个人总结里出现套话「{cliche}」",
                        "自我评价别写形容词，换成可验证的事实：做过什么、用什么方法、拿到什么结果",
                        cost=3, cap=12, line=s_line)
        for vague in VAGUE_WORDS:
            if vague in summary_text:
                stats["vague_hits"].append(vague)
                rep.add("套话", "low", "个人总结",
                        f"个人总结里出现模糊词「{vague}」",
                        f"把「{vague}」换成具体数字或范围", cost=1.5, cap=12,
                        line=s_line)

        # C10 自我评价公式：身份定位 + 核心能力 + 代表成果
        s_len = len(re.sub(r"\s", "", summary_text))
        has_identity = bool(IDENTITY_MARKERS.search(summary_text))
        has_ability = bool(METHOD_MARKERS.search(summary_text))
        has_result = bool(RESULT_MARKERS.search(summary_text)) or _has_quant(summary_text)
        if s_len < 40:
            rep.add("结构", "low", "个人总结", f"个人总结仅 {s_len} 字，写不下三要素",
                    "按「身份定位 + 2~3 项核心能力 + 1 个代表性成果」写到 60~120 字",
                    cost=2, cap=10, line=s_line)
        elif not has_identity and not has_ability:
            rep.add("结构", "low", "个人总结", "个人总结看不出你的身份定位与核心能力",
                    "开头先点明「我是谁 + 主攻什么方向」，再给 2~3 项拿得出手的能力",
                    cost=1.5, cap=10, line=s_line)
        elif not has_result:
            rep.add("结构", "low", "个人总结", "个人总结里没有代表性成果",
                    "补 1 个最有说服力的成果（数字优先；没有数字就用规模或影响）",
                    cost=1.5, cap=10, line=s_line)

    # --- 3. 每段经历的要点条数 ---
    for idx, exp in enumerate(doc.get("experiences") or [], start=1):
        label = exp.get("left") or f"第 {idx} 段经历"
        exp_line = exp.get("line")                 # 该段经历的标题行
        count = len(exp.get("bullets") or [])
        low, high = EXP_BULLET_RANGE
        if count == 0:
            rep.add("结构", "high", label, "只有标题没有要点",
                    "补 3~5 条要点：动词 + 对象 + 方法/技术 + 量化结果", cost=6, cap=15,
                    line=exp_line)
        elif count == 1:
            rep.add("结构", "medium", label, f"只有 {count} 条要点，内容过单薄",
                    f"扩充到 {low}~{high} 条，把技术细节与结果写开", cost=3, cap=15,
                    line=exp_line)
        elif count < low:
            rep.add("结构", "low", label, f"只有 {count} 条要点，略少于 {low} 条",
                    f"补到 {low}~{high} 条，让这段经历的说服力更完整", cost=1.5, cap=15,
                    line=exp_line)
        elif count > high:
            rep.add("结构", "low", label, f"有 {count} 条要点，略显冗长",
                    f"合并同类项，压到 {low}~{high} 条，每条只留最有说服力的信息", cost=2, cap=15,
                    line=exp_line)

    # --- 4. 逐条要点检查 ---
    total_len = 0
    period_issues = 0
    for label, bullet, b_line in bullets:
        length = len(bullet)
        total_len += length

        weak = _starts_weak(bullet)
        if weak:
            stats["weak_verb_count"] += 1
            rep.add("表达", "medium", label,
                    f"要点以弱动词「{weak}」开头：{bullet[:24]}…",
                    f"换成强动词开头，例如「{_suggest_rewrite(bullet, weak)[:40]}」"
                    + _quant_tip(bullet),
                    cost=3.5, cap=20, line=b_line)

        if _has_quant(bullet):
            stats["quantified_count"] += 1

        t_low, t_high = BULLET_TARGET
        if length > BULLET_HARD_MAX:
            rep.add("表达", "medium", label,
                    f"要点过长（{length} 字），HR 扫读时会跳过：{bullet[:24]}…",
                    f"拆成两条，或删掉铺垫词，压到 {t_low}~{t_high} 字", cost=2, cap=10,
                    line=b_line)
        elif length > t_high:
            rep.add("表达", "low", label,
                    f"要点略超上限（{length} 字 > {t_high}）",
                    f"去掉铺垫词，压到 {t_low}~{t_high} 字；一条只讲一件事", cost=1, cap=10,
                    line=b_line)
        elif length < BULLET_HARD_MIN:
            rep.add("表达", "low", label,
                    f"要点过短（{length} 字），信息量不足：{bullet}",
                    "补上「方法/技术」和「结果」，写满一句话", cost=1, cap=10,
                    line=b_line)
        elif length < t_low:
            rep.add("表达", "low", label,
                    f"要点略短（{length} 字 < {t_low}）",
                    "补一句方法或结果（用了什么技术、带来什么变化）", cost=0.8, cap=10,
                    line=b_line)

        # C1 四要素：既无「方法/技术」又无「结果」→ 大概率是只讲动作的流水账
        if not METHOD_MARKERS.search(bullet) and not RESULT_MARKERS.search(bullet):
            rep.add("表达", "medium", label,
                    f"要点只见动作、不见方法与结果：{bullet[:24]}…",
                    "按「强动词 + 对象 + 方法/技术 + 量化结果」补全，"
                    "例如补上用的技术栈、或带来的变化" + _quant_tip(bullet),
                    cost=2, cap=12, line=b_line)

        for cliche in CLICHES:
            if cliche in bullet:
                stats["cliche_hits"].append(cliche)
                rep.add("套话", "high", label,
                        f"出现 HR 反感的套话「{cliche}」",
                        "删掉自我评价式形容词，换成可验证的事实与数字", cost=3, cap=12,
                        line=b_line)
                break

        for vague in VAGUE_WORDS:
            if vague in bullet:
                stats["vague_hits"].append(vague)
                rep.add("套话", "low", label,
                        f"出现模糊词「{vague}」",
                        f"把「{vague}」替换为具体数字或范围", cost=1.5, cap=12,
                        line=b_line)
                break

        if bullet.endswith(("。", ".")) and not bullet.endswith(("...", "…")):
            period_issues += 1

    if period_issues:
        rep.add("格式", "low", "整体", f"{period_issues} 条要点末尾带了句号",
                "项目符号条目末尾不加句号，全篇统一", cost=min(period_issues, 4) * 0.8, cap=5)

    if bullets:
        stats["avg_bullet_len"] = round(total_len / len(bullets), 1)
        stats["quantified_ratio"] = round(stats["quantified_count"] / len(bullets), 3)
        if stats["quantified_ratio"] >= QUANT_TARGET:
            rep.highlights.append(
                f"量化率 {stats['quantified_ratio']:.0%}，达到成熟产品建议的 60% 线")
        else:
            rep.add("量化", "high", "整体",
                    f"量化率仅 {stats['quantified_ratio']:.0%}（{stats['quantified_count']}/{len(bullets)} 条要点含数字）",
                    "把「提升了效率」改成「效率提升 30%」；确无数字时用「支撑/覆盖/缩短排查时间」等可辩护表述，"
                    "切勿编造百分比", cost=10, cap=15)

    # --- 5. 全文篇幅 ---
    char_count = stats["char_count"]
    low, high = FULL_CHAR_RANGE
    if char_count and char_count < low:
        rep.add("篇幅", "medium", "整体", f"全文仅 {char_count} 字，低于 {low} 字下限",
                "把每段经历写满 3~5 条要点，补足技术细节与结果", cost=8, cap=10)
    elif char_count > high:
        rep.add("篇幅", "medium", "整体", f"全文 {char_count} 字，超出一页 A4 容量",
                "与目标岗位无关的经历精简或删除，优先保留量化的成果", cost=6, cap=10)
    else:
        rep.highlights.append(f"全文 {char_count} 字，落在一页 A4 的合理区间")

    # --- 6. 技能 ---
    skills = doc.get("skills") or []
    if skills:
        if len(skills) < 6:
            rep.add("结构", "low", "技能", f"技能仅 {len(skills)} 项，偏少",
                    "补充到 8~20 项，按「语言 / 框架与库 / 工具与平台」分组", cost=4, cap=10)
        elif len(skills) > 25:
            rep.add("结构", "low", "技能", f"技能 {len(skills)} 项，过于堆砌",
                    "删掉与目标岗位无关的技能，保 8~20 项", cost=3, cap=10)
        dup = {s for s in skills if skills.count(s) > 1}
        if dup:
            rep.add("格式", "low", "技能", "技能存在重复：" + "、".join(sorted(dup)[:5]),
                    "去重后按分组呈现", cost=2, cap=5)

    # --- 7. 标点规范 ---
    if re.search(r"[\u4e00-\u9fff]\s*[,.;:]\s*[\u4e00-\u9fff]", raw):
        rep.add("格式", "low", "整体", "中文句子里混用了英文标点",
                "中文内容用中文标点（，。；：），英文术语保留原样", cost=2, cap=5)

    # --- 7b. 发展连续性（时间线：空窗 / 重叠 / 倒挂 / 经历早于入学） ---
    try:
        cont = continuity.evaluate_continuity(doc)
        stats["continuity"] = {
            "score": cont["score"],
            "issues": cont["issues"],
            "gaps": cont["gaps"],
            "overlaps": cont["overlaps"],
            "timeline": [
                {"label": t["label"], "kind": t["kind"], "group": t["group"],
                 "raw_period": t["raw_period"], "start": t["start"], "end": t["end"]}
                for t in cont["timeline"]
            ],
            "progression": cont["progression"],
        }
        _cost = {"high": 5.0, "medium": 3.0, "low": 1.5}
        for issue in cont["issues"]:
            rep.add("连续性", issue["severity"], issue["location"],
                    issue["message"], issue["fix"],
                    cost=_cost.get(issue["severity"], 2.0), cap=16)
        if cont["dated_count"] >= 2 and cont["score"] >= 95:
            rep.highlights.append("时间线连贯，未发现空窗、重叠或时间倒挂")
    except Exception:
        pass

    # --- 8. JD 关键词覆盖 ---
    if (jd_text or "").strip():
        try:
            from .ats import check_ats_compatibility
            result = check_ats_compatibility(raw or "", jd_text)
            matched = result.get("matched_keywords") or []
            missing = result.get("missing_keywords") or []
            total = len(matched) + len(missing)
            if total:
                coverage = len(matched) / total
                stats["jd_coverage"] = round(coverage, 3)
                if coverage < 0.5:
                    rep.add("关键词", "high", "整体",
                            f"JD 关键词命中率 {coverage:.0%}",
                            "用 JD 的术语重述你确实掌握的能力：" + "、".join(missing[:8]),
                            cost=8, cap=12)
                elif coverage < 0.75:
                    rep.add("关键词", "medium", "整体",
                            f"JD 关键词命中率 {coverage:.0%}",
                            "补齐以下关键词的自然表述：" + "、".join(missing[:8]),
                            cost=4, cap=12)
                else:
                    rep.highlights.append(f"JD 关键词命中率 {coverage:.0%}，ATS 友好")
        except Exception:
            pass

    # --- 9. 相关性排序（C8）+ 关键词堆砌（C11）---
    if (jd_text or "").strip():
        try:
            # 复用 ats 的 JD 需求词抽取（技能词典 + 英文技术词），保证与 ATS 分同一口径
            from .ats import _jd_requirements, _match_skill_in_text
            from .text_utils import normalize_text

            reqs = _jd_requirements(jd_text)
            if reqs:
                exps = doc.get("experiences") or []
                if len(exps) >= 2:
                    hits: List[int] = []
                    for exp in exps:
                        segment = " ".join([exp.get("left") or ""] + list(exp.get("bullets") or []))
                        seg_norm = normalize_text(segment)
                        hits.append(sum(1 for r in reqs if _match_skill_in_text(r, seg_norm)))
                    stats["jd_hits_by_experience"] = hits
                    best = max(range(len(hits)), key=lambda i: hits[i])
                    # 触发条件刻意收紧，避免误伤「按时间倒序」这种正常写法
                    if best > 0 and hits[0] == 0 and hits[best] >= RELEVANCE_GAP:
                        rep.add("关键词", "medium",
                                exps[best].get("left") or f"第 {best + 1} 段经历",
                                f"最贴合 JD 的是第 {best + 1} 段（命中 {hits[best]} 个 JD 关键词），"
                                "却不在最前面；首段命中 0 个",
                                "把与目标岗位最相关的经历提到最前面——HR 与 ATS 都从上往下看",
                                cost=3, cap=8)

                # 关键词堆砌：同一关键词全篇出现次数过多
                text_norm = normalize_text(raw or "")
                overused: List[str] = []
                for req in reqs:
                    token = normalize_text(req).strip()
                    if len(token) < 2:
                        continue
                    repeats = text_norm.count(token)
                    if repeats > KEYWORD_MAX_REPEAT:
                        overused.append(f"{req}×{repeats}")
                if overused:
                    rep.add("关键词", "low", "整体",
                            "关键词出现次数偏多，有堆砌嫌疑：" + "、".join(sorted(set(overused))[:5]),
                            "同一关键词全篇自然出现 2~3 次即可，堆砌会被 HR 与 ATS 判为刻意优化",
                            cost=2, cap=8)
        except Exception:
            pass

    stats["cliche_hits"] = sorted(set(stats["cliche_hits"]))
    stats["vague_hits"] = sorted(set(stats["vague_hits"]))

    # --- 10. 表达库：给最需要改的几条要点产出「原句 → 建议句」 ---
    try:
        rewrite_suggestions = expression_bank.enrich([b for _, b in bullets], limit=3)
    except Exception:
        rewrite_suggestions = []

    rep.issues.sort(key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(i["severity"], 3))
    # 给每条问题补上"原文那一行"的片段：光给一个行号不好用，
    # 配上原文，三个消费点（生成页卡片、优化页质检列表、导出的摘要）
    # 都能让用户直接对上。没行号（结构化路径/整篇级结论）就是空串。
    for issue in rep.issues:
        issue["snippet"] = line_text(raw, issue.get("line"))
    score = rep.score
    level = "优秀" if score >= 90 else "良好" if score >= 80 else "及格" if score >= 70 else "偏弱"
    return {
        "score": score,
        "level": level,
        "issues": rep.issues,
        "stats": stats,
        "highlights": rep.highlights,
        "rewrite_suggestions": rewrite_suggestions,
    }


def review_structured(structured: Dict[str, Any], jd_text: str = "") -> Dict[str, Any]:
    return review_resume(structured=structured, text="", jd_text=jd_text)


def review_text(text: str, jd_text: str = "") -> Dict[str, Any]:
    return review_resume(structured=None, text=text, jd_text=jd_text)


def summarize_issues(report: Dict[str, Any], limit: int = 6) -> str:
    """把问题清单压成一行摘要，供 UI 直接展示。

    有行号的条目带上「第 N 行」——摘要经常被单独贴到别处（导出、日志、
    Agent 的上下文）看，丢掉行号等于把这些场景里的可定位性也一起丢了。
    """
    issues = (report or {}).get("issues") or []
    if not issues:
        return "未发现内容问题"
    parts = []
    for i in issues[:limit]:
        loc = i.get("location") or ""
        if i.get("line"):
            loc = f"第 {i['line']} 行·{loc}" if loc else f"第 {i['line']} 行"
        parts.append(f"{loc}：{i['message']}")
    more = f"（另有 {len(issues) - limit} 条）" if len(issues) > limit else ""
    return "；".join(parts) + more
