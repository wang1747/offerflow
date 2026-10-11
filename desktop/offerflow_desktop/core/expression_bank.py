"""简历表达库：把「这句话怎么写更好」变成可执行的改写建议。

`resume_review` 负责**判定**问题（弱动词、缺量化、只见动作不见结果），
本模块负责**给出改法**——补上「判定之后怎么办」这一环：

* 强动词词库（按领域分组的动作词，避免全篇都是「负责/参与」）
* 弱动词 → 强动词升级表
* 指标维度提示（写了「优化」但没写优化了什么指标，该补时延还是吞吐？）
* 要点句式模板（方法 → 动作 → 结果 的骨架）
* ``rewrite`` / ``enrich`` 对外入口，直接产出可抄的建议文本

设计取向：**建议必须可落地**。所以每条建议都要带上「原句 → 建议句」，
并且示例里保留原句的技术内容，只改动词与结构，不凭空编造事实。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

# ============ 强动词词库（按领域分组） ============

DOMAIN_VERBS: Dict[str, List[str]] = {
    "技术研发": [
        "主导", "搭建", "重构", "落地", "实现", "开发", "设计", "引入", "迁移",
        "抽象", "解耦", "封装", "调优", "压测", "上线", "部署", "攻坚", "打通",
    ],
    "数据算法": [
        "建模", "训练", "调优", "评估", "抽取", "清洗", "对齐", "召回", "排序",
        "量化", "验证", "复现", "迭代", "定位", "归因", "沉淀",
    ],
    "性能优化": [
        "优化", "降低", "缩短", "提升", "压缩", "削减", "加速", "扩容", "缩容",
        "降级", "限流", "缓存", "预热", "并行化", "批处理",
    ],
    "协作管理": [
        "牵头", "协同", "推动", "推进", "拆解", "排期", "对齐", "评审", "把关",
        "沉淀", "复盘", "培训", "带教", "统筹",
    ],
    "产品运营": [
        "策划", "设计", "调研", "洞察", "验证", "拉新", "促活", "转化", "复盘",
        "制定", "建立", "输出", "复制",
    ],
    "通用": [
        "独立", "持续", "系统化", "体系化", "端到端", "从零到一",
    ],
}

# 弱动词 → 推荐强动词（首个为默认，其余为备选）
VERB_UPGRADE: Dict[str, List[str]] = {
    "负责": ["主导", "牵头", "承接", "落地"],
    "参与": ["主导", "深度参与", "承担核心模块"],
    "协助": ["协同推进", "支撑", "配合完成（并说明自己负责的部分）"],
    "帮助": ["支撑", "赋能", "带动"],
    "配合": ["协同", "对齐", "联动"],
    "完成": ["交付", "落地", "按期交付"],
    "跟进": ["推进", "闭环", "跟踪至交付"],
    "支持": ["支撑", "保障", "承载"],
    "从事": ["深耕", "承担"],
    "做了": ["实现", "搭建", "落地"],
    "搞": ["实现", "落地"],
    "写": ["撰写", "输出", "沉淀"],
    "弄": ["实现", "处理"],
    "参加": ["参与", "主导"],
    "听从": ["承接", "执行"],
}

# 弱动词全集（供检测）
WEAK_VERBS: Tuple[str, ...] = tuple(sorted(VERB_UPGRADE, key=len, reverse=True))

# 指标维度提示：命中左侧特征词 → 建议补右侧指标
_METRIC_RULES: List[Tuple[str, str, List[str]]] = [
    (r"优化|性能|慢|卡顿|耗时|延迟|响应", "性能与效率",
     ["接口 P95 时延", "QPS/TPS", "CPU/内存占用", "构建耗时", "首屏加载时间"]),
    (r"测试|用例|覆盖率|质量|缺陷|bug|BUG", "质量",
     ["测试用例数", "覆盖率", "缺陷密度", "回归通过率", "线上故障数"]),
    (r"重构|解耦|迁移|架构|模块", "可维护性",
     ["模块数", "依赖数", "重复代码率", "改动影响面", "编译/构建耗时"]),
    (r"检索|召回|推荐|模型|算法|准确|效果", "模型效果",
     ["准确率", "召回率", "F1", "AUC", "Top-K 命中率"]),
    (r"数据|表|库|存储|清洗|抽取", "数据规模",
     ["表数量", "实体类型数", "记录条数", "数据量(GB/TB)", "字段数"]),
    (r"文档|方案|规范|流程|知识库|培训", "沉淀与复用",
     ["篇数", "覆盖人数", "被引用次数", "评审通过率", "节省工时"]),
    (r"用户|客户|运营|增长|转化|留存", "业务增长",
     ["用户数", "转化率", "留存率", "GMV", "活跃度"]),
    (r"上线|部署|发布|交付|迭代|版本", "交付节奏",
     ["上线次数", "迭代周期", "交付准时率", "可用性(SLA)"]),
    (r"团队|协作|沟通|配合|对接", "协作规模",
     ["协作人数", "跨部门数", "对接方数量", "会议/评审次数"]),
    (r"降低|节省|缩减|成本", "成本",
     ["成本降幅(%)", "节省金额", "人力节省(人天)"]),
]

# 要点句式骨架（方法 → 动作 → 结果）
BULLET_PATTERNS: List[str] = [
    "通过{方法}，{动作}{对象}，{指标}从 {A} 提升到 {B}",
    "基于{技术}搭建{对象}，支撑{规模}，{指标}优于{基线}",
    "{动作}{对象}，覆盖{范围}，{指标}达到 {数值}",
    "针对{问题}，{动作}{对象}，{指标}降低 {幅度}",
]

# 个人总结骨架（身份定位 + 核心能力 + 代表成果）
SUMMARY_TEMPLATE = (
    "{届别}届{专业}{学历}，{方向定位}方向，"
    "熟悉{核心能力1}与{核心能力2}；"
    "代表成果：{代表成果}。"
)

# 职责递进标记（越靠后等级越高）
_SENIORITY_MARKERS: Tuple[Tuple[str, int], ...] = (
    ("实习", 1), ("助理", 1), ("参与", 1), ("协助", 1),
    ("初级", 2), ("工程师", 3), ("开发", 3), ("独立", 3),
    ("高级", 4), ("资深", 4), ("核心", 4),
    ("主导", 5), ("负责人", 5), ("牵头", 5), ("组长", 5), ("主管", 5),
    ("架构", 6), ("经理", 6), ("leader", 6), ("负责人", 5),
)


# ============ 工具函数 ============

def detect_weak_verb(text: str) -> Optional[str]:
    """返回句子开头的弱动词（没有则 None）。"""
    head = (text or "").strip()
    for verb in WEAK_VERBS:
        if head.startswith(verb):
            return verb
    return None


def upgrade_verb(verb: str, index: int = 0) -> str:
    options = VERB_UPGRADE.get(verb)
    if not options:
        return verb
    return options[min(index, len(options) - 1)]


def detect_domain(text: str) -> str:
    """按关键词猜测文本所属领域，用于挑选贴合的动作词。"""
    text = text or ""
    rules = [
        ("数据算法", r"模型|算法|训练|数据|检索|召回|特征|指标|SQL|分析"),
        ("性能优化", r"性能|优化|时延|QPS|并发|缓存|内存|加载|压测"),
        ("产品运营", r"运营|用户|增长|转化|活动|策划|调研|需求|市场"),
        ("协作管理", r"团队|协作|管理|排期|牵头|协同|评审|推进"),
        ("技术研发", r"开发|架构|接口|服务|系统|模块|重构|部署|上线|代码"),
    ]
    for name, pattern in rules:
        if re.search(pattern, text, re.I):
            return name
    return "技术研发"


def suggest_verbs(text: str, n: int = 6) -> List[str]:
    """按领域推荐一批强动词（优先当前句没用过的）。"""
    domain = detect_domain(text)
    pool = list(DOMAIN_VERBS.get(domain, [])) + list(DOMAIN_VERBS["通用"])
    used = {v for v in DOMAIN_VERBS.get(domain, []) if v in (text or "")}
    # 去重保序
    out: List[str] = []
    for verb in pool:
        if verb not in out and verb not in used:
            out.append(verb)
    return out[:n]


def metric_hints(text: str, limit: int = 3) -> List[str]:
    """根据句子内容推荐该补哪些指标。"""
    text = text or ""
    out: List[Tuple[int, str, List[str]]] = []
    for pattern, name, metrics in _METRIC_RULES:
        hits = len(re.findall(pattern, text, re.I))
        if hits:
            out.append((hits, name, metrics))
    out.sort(key=lambda x: x[0], reverse=True)
    return [f"{name}：{'、'.join(metrics[:4])}" for _, name, metrics in out[:limit]]


def seniority(text: str) -> int:
    """粗估一段描述的职责等级（0~6）。"""
    text = text or ""
    level = 0
    for marker, value in _SENIORITY_MARKERS:
        if marker.lower() in text.lower():
            level = max(level, value)
    return level


def domain_verbs(domain: str) -> List[str]:
    return list(DOMAIN_VERBS.get(domain, [])) + list(DOMAIN_VERBS["通用"])


def domains() -> List[str]:
    return list(DOMAIN_VERBS)


# ============ 对外入口：改写建议 ============

def rewrite(bullet: str, *, verb_index: int = 0) -> Dict[str, Any]:
    """给一条要点产出改写建议。

    返回 {original, issue, suggestion, verb, metrics, pattern}。
    只在「弱动词开头」时替换动词，其余情况保留原句内容、只补结构建议，
    绝不凭空编造事实。
    """
    original = (bullet or "").strip()
    if not original:
        return {"original": "", "issue": "", "suggestion": "", "verb": "",
                "metrics": [], "pattern": ""}

    weak = detect_weak_verb(original)
    suggestion = original
    issue_parts: List[str] = []

    if weak:
        strong = upgrade_verb(weak, verb_index)
        suggestion = strong + original[len(weak):]
        issue_parts.append(f"开头弱动词「{weak}」→「{strong}」")

    has_number = bool(re.search(r"\d", original))
    metrics = metric_hints(original)
    if not has_number and metrics:
        issue_parts.append("补一个量化指标")

    pattern = ""
    if not has_number:
        # 只给骨架，让用户自己填事实
        tech = detect_domain(original)
        pattern = BULLET_PATTERNS[0]
        if pattern:
            issue_parts.append("套用「方法 → 动作 → 结果」骨架")

    return {
        "original": original,
        "issue": "；".join(issue_parts),
        "suggestion": suggestion,
        "verb": upgrade_verb(weak, verb_index) if weak else "",
        "metrics": metrics,
        "pattern": pattern,
        "domain": detect_domain(original),
        "verbs": suggest_verbs(original),
        "has_quant": has_number,
    }


def enrich(bullets: List[str], limit: int = 3) -> List[Dict[str, Any]]:
    """从一批要点里挑出最需要改的几条，给出改写建议。

    排序优先级：弱动词开头 > 无量化 > 缺方法与结果。
    """
    scored: List[Tuple[int, str]] = []
    for bullet in bullets or []:
        text = (bullet or "").strip()
        if not text:
            continue
        weight = 0
        if detect_weak_verb(text):
            weight += 3
        if not re.search(r"\d", text):
            weight += 2
        if not re.search(r"通过|基于|借助|采用|使用|利用", text):
            weight += 1
        if weight:
            scored.append((weight, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [rewrite(text) for _, text in scored[:limit]]


def summary_hint(structured: Dict[str, Any]) -> Dict[str, Any]:
    """给出个人总结的改写骨架，并把已知信息填进去。"""
    structured = structured or {}
    education = structured.get("education") or []
    school_info = education[0] if education and isinstance(education[0], dict) else {}
    degree = str(school_info.get("degree") or "本科").strip()
    major = str(school_info.get("major") or "本专业").strip()
    grade = ""
    start = str(school_info.get("start") or "")
    match = re.search(r"(19|20)(\d{2})", start)
    if match:
        grade = str(int(match.group(1) + match.group(2)) + 4)

    position = str(structured.get("position") or "").strip()
    skills = [s for s in (structured.get("skills") or []) if isinstance(s, str)][:2]
    skeleton = SUMMARY_TEMPLATE.format(
        届别=grade or "20XX",
        专业=major,
        学历=degree,
        方向定位=position or "目标岗位",
        核心能力1=skills[0] if skills else "核心技能一",
        核心能力2=skills[1] if len(skills) > 1 else "核心技能二",
        代表成果="独立交付 X 个项目，其中 Y 项指标提升 Z%",
    )
    return {
        "skeleton": skeleton,
        "tips": [
            "身份定位：届别 + 专业 + 学历 + 方向，一句话交代你是谁",
            "核心能力：只写 2~3 项与岗位最相关、且能拿出证据的",
            "代表成果：必须有数字，宁可写小数字也不要写「表现良好」",
            "避免套话：吃苦耐劳 / 学习能力强 / 有责任心 一律删掉",
        ],
    }
