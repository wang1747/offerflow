"""应届生市场薪资基准表（2026 届）——自 Web 版完整移植。

核心原则：薪资数字由真实市场数据 + 规则计算，不由 LLM 编造。
LLM 只负责从简历提取「学历 / 学校层次 / 求职方向 / 城市」，本模块据此查表算区间。

Web 版的基准数据落库版本化（`salary_benchmark_store`）；桌面版对等实现见
`core/salary_benchmark_store.py`：生效数据存在本机 `local_store`，本模块的所有
查表函数都接受 `data=` 参数按生效数据计算，缺省回退本文件的内置常量。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .text_utils import alias_in_text

#: ⚠️ 口径提示（容易踩的坑）——本表 `DEGREE_BASE` 是「**全市场**应届生起薪
#: **按学历的分布**」：硕士生更多流向高薪岗位（算法/研发），所以学历档之间差异
#: 天然较大（硕士 ≈ 普通本科 1.7 倍）。这与「**同一岗位内**的学历微调系数」
#: （常见 1.05~1.15 倍）**不是一回事**，两者不可互相换算、也不能拿后者来校准本表。
DATA_SOURCES: List[str] = [
    "智联猎头《2026 年度企业薪酬调研报告》（应届生起薪，分学历/院校/城市）",
    "福睿思特《2026 年 IT 互联网/软件开发行业薪资水平报告》（分岗位/城市）",
    "麦可思《2026 年中国本科生就业蓝皮书》（分城市月收入）",
    "新东方《2026 届校招薪资调查报告》（28.6 万样本，分行业/学历/城市）",
    "说明：以上为公开薪酬报告的**量级参考**；学历/城市/方向三张表之间的"
    "**相对关系**按上述报告的结构设定，并非逐项实测值。",
    # 口径说明必须是**运行时字符串**（上面的 `#:` 注释人看得到、但注释不进产物，
    # 拆包校验与界面都拿不到）——见 2026-10-10 踩坑：口径说明只写在注释里，
    # 拆包判据 `has_text("全市场")` 恒 FAIL，是判据空靶而非代码缺失。
    "口径说明：本表是「应届生起薪按学历的**全市场分布**」（硕士 ≈ 普通本科 1.7 倍），"
    "与「**同一岗位内**的学历微调系数」（常见 1.05~1.15 倍）不是一回事，"
    "两者不可互相换算，也不能拿后者来校准本表。",
]

DEGREE_BASE: Dict[str, Tuple[int, int]] = {
    "associate":       (4000, 6000),
    "bachelor_normal": (6000, 9000),
    "bachelor_elite":  (8000, 13000),
    "master":          (10000, 16000),
    "phd":             (22000, 30000),
}

DEGREE_LABELS: Dict[str, str] = {
    "associate":       "大专",
    "bachelor_normal": "本科（普通院校）",
    "bachelor_elite":  "本科（985/211）",
    "master":          "硕士",
    "phd":             "博士",
}

DIRECTION_COEF: Dict[str, Tuple[float, str]] = {
    "ai":          (1.45, "AI/算法/大模型"),  # 2026-10-11 校准：原 1.70 是「含硕士流向」的全市场分布口径，套本科应届偏高。按 1.45 计，一线城市普通本科应届 = 8.5k~13.0k（目标价 13k），985 本科 11.5k~19.0k，保留合理溢价
    "frontend":    (1.25, "前端开发"),
    "tech":        (1.35, "技术研发"),
    "chip":        (1.45, "半导体/芯片/硬件"),
    "product":     (1.05, "产品经理"),
    "design":      (0.95, "设计"),
    "operation":   (0.90, "运营/新媒体"),
    "marketing":   (0.90, "市场/销售"),
    "finance":     (1.05, "金融/财会"),
    "legal":       (0.95, "法律/法务"),
    "admin":       (0.85, "行政/人事/文职"),
    "education":   (0.85, "教育/教师"),
    "medical":     (0.95, "医疗/护理/医药"),
    "mechanical":  (0.95, "机械/制造"),
    "electrical":  (1.00, "电气/自动化"),
    "civil":       (0.90, "土木/建筑"),
    "media":       (0.85, "传媒/新闻"),
    "environment": (0.85, "环境/环保"),
    "logistics":   (0.85, "物流/采购/供应链"),
    "general":     (0.90, "通用/其他"),
}

CITY_TIERS: Dict[str, Tuple[float, str]] = {
    "tier1": (1.00, "一线城市（北上广深）"),
    "tier2": (0.85, "新一线 / 强二线（杭州、成都、南京、武汉、苏州等）"),
    "tier3": (0.72, "二三线城市"),
}

_TIER1_CITIES = {"北京", "上海", "深圳", "广州"}
_TIER2_CITIES = {
    "杭州", "成都", "南京", "武汉", "苏州", "西安", "重庆", "天津",
    "长沙", "郑州", "青岛", "合肥", "东莞", "佛山", "宁波", "无锡",
    "厦门", "福州", "济南", "大连", "沈阳", "昆明", "南昌", "哈尔滨",
}

_DIRECTION_LABEL_MAP: Dict[str, str] = {
    "AI/算法/大模型": "ai",
    "前端开发": "frontend",
    "技术研发": "tech",
    "半导体/芯片/硬件": "chip",
    "产品经理": "product",
    "设计": "design",
    "运营/新媒体": "operation",
    "市场/销售": "marketing",
    "金融/财会": "finance",
    "法律/法务": "legal",
    "行政/人事/文职": "admin",
    "教育/教师": "education",
    "医疗/护理/医药": "medical",
    "机械/制造": "mechanical",
    "电气/自动化": "electrical",
    "土木/建筑": "civil",
    "传媒/新闻": "media",
    "环境/环保": "environment",
    "物流/采购/供应链": "logistics",
    "通用/其他": "general",
}

#: 方向关键词表（**顺序即优先级**：自上而下、先命中先返回）。
#: 约定：
#: * ASCII 关键词按**词边界**匹配（见 ``text_utils.alias_in_text``）——
#:   ``ai`` 不得命中 ``email``/``training``、``ic`` 不得命中 ``curriculum``；
#: * CJK 关键词按子串匹配（中文没有词边界）。
#:
#: ⚠️ 2026-10-11 精度修正（A）——**宽词剔除**：
#: * 删 ``ai`` 的 ``智能``：它把 ``智能制造工程师``/``智能硬件`` 抢成 ai
#:   （应分别为机械/芯片；两词都含「智能」只是"智能化"修饰）。改为只保留
#:   ``智能驾驶``/``智能语音`` 等**明确复合词**。
#: * 删 ``ai`` 的 ``cv``：与「投递用 CV（简历）」撞车，改由 ``计算机视觉`` /
#:   ``机器视觉`` / ``computer vision`` 覆盖。
#: * **没有**采用「把 ai 整体后移」的重排方案：``civil`` 含超宽泛的「工程」、
#:   ``mechanical``/``civil`` 都含「结构」，一前移会把 ``软件工程``/``算法工程师``
#:   整片吞掉 —— 重排的收益与代价不对称，改词更安全。
#:
#: ⚠️ 2026-10-11 覆盖修正（B）——**补英文岗位角色词**：此前 ``_DIRECTION_KEYWORDS``
#: 只有中文角色词，``Frontend Engineer``/``Backend Engineer``/``Data analyst``
#: 等英文岗位名会一律落到 ``general``，给非 AI 岗算出错误区间。
_DIRECTION_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("ai", ["ai", "人工智能", "算法", "大模型", "机器学习", "深度学习", "自然语言", "nlp",
            "计算机视觉", "机器视觉", "数据科学", "推荐算法", "推荐系统",
            "智能驾驶", "智能座舱", "智能语音", "智能推荐", "智能问答", "智能体",
            "llm", "rag", "langchain", "pytorch", "tensorflow",
            "machine learning", "deep learning", "data scientist", "data science",
            "ai engineer", "ml engineer", "nlp engineer", "computer vision",
            "algorithm engineer", "prompt engineering"]),
    ("chip", ["芯片", "半导体", "集成电路", "ic", "硬件", "fpga", "微电子", "嵌入式硬件",
              "hardware", "semiconductor", "chip", "asic"]),
    # mechanical 必须排在 tech **之前**：tech 含超宽泛的「工程师」，
    # 排在前面会把「智能制造工程师」抢成 tech（应机械）——见 A 的探针实测。
    # mechanical 自身词（机械/制造/工艺/结构/模具…）不会反向吞掉软件岗。
    ("mechanical", ["机械", "制造", "工艺", "结构", "模具", "装配", "机加工", "cad制图",
                    "mechanical", "manufacturing", "solidworks", "ansys", "cnc"]),
    ("frontend", ["前端", "web前端", "大前端", "h5", "web", "小程序",
                  "frontend", "front end", "front-end", "web developer",
                  "react developer", "vue developer"]),
    ("tech", ["技术研发", "研发", "技术", "后端", "开发", "工程师", "java", "python", "go", "golang",
              "c++", "测试", "运维", "大数据", "数据分析", "数据库", "安卓", "android",
              "ios", "客户端", "全栈", "程序员", "软件", "云计算", "sre", "devops",
              "backend", "back end", "back-end", "full stack", "fullstack",
              "software engineer", "software developer", "developer", "programmer",
              "data analyst", "data engineer", "qa engineer", "test engineer",
              "cloud engineer", "mobile developer", "android developer", "ios developer"]),
    ("product", ["产品", "pm", "需求分析", "product manager", "product owner"]),
    ("design", ["设计", "ui", "ux", "视觉", "交互", "平面", "美工", "原画",
                "designer", "ui designer", "ux designer"]),
    ("operation", ["运营", "新媒体", "内容", "社群", "用户增长", "电商运营",
                   "content operation", "community manager"]),
    ("marketing", ["市场", "销售", "商务", "bd", "客户经理", "渠道", "品牌", "营销", "推广",
                   "sales", "marketing", "business development", "account manager"]),
    ("finance", ["金融", "财会", "会计", "财务", "审计", "银行", "证券", "基金",
                 "投资", "风控", "税务", "出纳", "精算",
                 "finance", "financial analyst", "accountant", "audit", "actuary"]),
    ("legal", ["法律", "法务", "律师", "合规", "知识产权",
               "legal", "lawyer", "paralegal", "compliance"]),
    ("admin", ["行政", "人事", "hr", "人力", "文员", "文秘", "文职", "助理", "前台",
               "档案", "考勤", "招聘专员",
               "human resources", "recruiter", "administrative", "secretary"]),
    ("education", ["教师", "老师", "教育", "培训", "讲师", "课程", "教研", "幼师",
                   "teacher", "tutor", "lecturer"]),
    ("medical", ["医疗", "护理", "护士", "医药", "临床", "药剂", "医学", "健康管理",
                 "nurse", "clinic", "pharmacist", "medical"]),
    ("electrical", ["电气", "自动化", "电力", "plc", "继电", "供配电", "变频", "单片机",
                    "electrical", "automation", "power engineer"]),
    ("civil", ["土木", "建筑", "工程", "造价", "结构", "施工", "监理", "勘察", "测绘", "bim",
               "civil engineer", "structural engineer", "construction", "revit"]),
    ("media", ["传媒", "新闻", "编辑", "记者", "影视", "编导", "摄影", "文案", "主持", "播音",
               "journalist", "editor", "reporter", "copywriter"]),
    ("environment", ["环境", "环保", "环评", "水处理", "固废", "废气", "生态", "监测",
                     "environmental", "esg", "water treatment"]),
    ("logistics", ["物流", "采购", "供应链", "仓储", "运输", "快递", "库存",
                   "logistics", "supply chain", "procurement"]),
]

#: 简历侧（`core/skill_keywords.DIRECTION_SKILLS`）与薪资侧方向 key 的**同义异名**桥接（C）。
#: 同一条目两端曾各起一名：简历侧 ``environmental``（label「环境工程」）↔
#: 薪资侧 ``environment``（label「环境/环保」）。若把简历侧 key 直接喂给
#: `estimate_salary_range`，会因不在 `DIRECTION_COEF` 里而**静默回退 general**（白丢系数）。
#: 另：薪资侧本就多出 admin/chip/frontend/logistics 等简历侧未收录的细分方向 ——
#: 两集合是「薪资 ⊇ 简历」的超集关系，此表只桥接**同义异名**，不做集合合并。
_DIRECTION_KEY_ALIASES: Dict[str, str] = {
    "environmental": "environment",
}


def normalize_direction_key(key: str) -> str:
    """把简历侧/历史命名的方向 key 归一到薪资侧 key；无法识别时原样返回。"""
    k = (key or "").strip()
    return _DIRECTION_KEY_ALIASES.get(k, k)

_ELITE_SCHOOL_KEYWORDS = ["985", "211", "双一流", "c9", "清北", "top2"]
_ASSOCIATE_KEYWORDS = ["大专", "专科", "高职", "职业技术学院", "专科学校"]


def _round_to_500(value: float) -> int:
    return int(round(value / 500.0)) * 500


def resolve_degree(raw: str) -> str:
    text = (raw or "").strip()
    if any(k in text for k in ["博士", "phd", "博士后"]):
        return "phd"
    if any(k in text for k in ["硕士", "研究生", "master"]):
        return "master"
    if any(k in text for k in _ASSOCIATE_KEYWORDS) or text in ("大专", "专科"):
        return "associate"
    if any(k in text for k in _ELITE_SCHOOL_KEYWORDS):
        return "bachelor_elite"
    return "bachelor_normal"


def resolve_school_tier(raw: str) -> str:
    text = (raw or "").strip()
    if any(k in text for k in _ELITE_SCHOOL_KEYWORDS):
        return "elite"
    if any(k in text for k in _ASSOCIATE_KEYWORDS):
        return "associate"
    return "normal"


def resolve_direction(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "general"
    for label, key in _DIRECTION_LABEL_MAP.items():
        if label in text:
            return key
    lowered = text.lower()
    # ① 方向 key 直通（LLM 有时直接吐 key：``frontend`` / ``environment`` …）；
    # ② 简历侧同义异名归一（``environmental`` → ``environment``，见 _DIRECTION_KEY_ALIASES）。
    if lowered in _DIRECTION_KEY_ALIASES:
        return _DIRECTION_KEY_ALIASES[lowered]
    if lowered in DIRECTION_COEF:
        return lowered
    for key, keywords in _DIRECTION_KEYWORDS:
        for kw in keywords:
            # ASCII 关键词按**词边界**匹配：``ai`` 不得命中 ``email``/``training``、
            # ``ic`` 不得命中 ``curriculum``（子串匹配会把英文岗位名大量误判到 ai/chip，
            # 从而套上 1.45 的高系数）。CJK 关键词仍按子串（中文没有词边界）。
            if alias_in_text(kw, lowered):
                return key
    return "general"


def resolve_city_tier(city: str) -> str:
    c = (city or "").strip()
    if not c:
        return "tier1"
    for suffix in ("市", "省"):
        if c.endswith(suffix):
            c = c[:-1]
    if any(name in c for name in _TIER1_CITIES):
        return "tier1"
    if any(name in c for name in _TIER2_CITIES):
        return "tier2"
    return "tier3"


def experience_coef(years: float) -> float:
    """经验年限 -> 薪资上浮系数。0 年=应届=1.0；年限越长系数越高。"""
    if years < 0.5:
        return 1.0
    if years < 3:
        return 1.3
    if years < 5:
        return 1.6
    return 2.0


def experience_label(years: float) -> str:
    """经验年限 -> 中文标签（用于定薪依据与解读）。"""
    if years < 0.5:
        return "应届"
    return f"{years:g} 年经验"


def estimate_salary_range(degree: str, direction: str, city_tier: str,
                          data: Optional[Dict] = None,
                          experience: float = 0.0) -> Tuple[int, int]:
    """根据学历、岗位方向、城市档、经验年限算税前月薪区间 [low, high]。

    `data` 可选：传入**当前生效**的基准数据
    （`{"degree_base":…, "direction_coef":…, "city_tiers":…}`，见 `salary_benchmark_store`），
    缺省时回退内置常量（2026 届调研基线）。
    `experience`：工作年限，>0 时在应届起薪基础上按经验系数上浮。
    """
    degree_base = (data or {}).get("degree_base") or DEGREE_BASE
    direction_coef = (data or {}).get("direction_coef") or DIRECTION_COEF
    city_tiers = (data or {}).get("city_tiers") or CITY_TIERS

    degree = degree if degree in degree_base else "bachelor_normal"
    direction = normalize_direction_key(direction)
    direction = direction if direction in direction_coef else "general"
    city_tier = city_tier if city_tier in city_tiers else "tier1"

    base_low, base_high = degree_base[degree]
    coef, _ = direction_coef[direction]
    city_coef, _ = city_tiers[city_tier]
    exp_coef = experience_coef(experience)

    low = _round_to_500(base_low * coef * city_coef * exp_coef)
    high = _round_to_500(base_high * coef * city_coef * exp_coef)
    if high <= low:
        high = low + 500
    return low, high


# 开口价上浮系数：谈判锚定策略 —— 开口价要高于市场价上限，给 HR 压价留出空间。
# 开口价=上限时，HR 一压就跌破真实期望，等于开局即失守。
_ASK_PREMIUM = 1.15


def suggest_target_and_bottom(degree: str, direction: str, city_tier: str,
                              data: Optional[Dict] = None,
                              experience: float = 0.0) -> Tuple[int, int, int]:
    """给出谈判建议：开口价（报价锚点）、目标价（期望落地）、底线价。

    返回 `(ask, target, bottom)`，与 Web 版 `suggest_target_and_bottom` 同序同义：
    - 开口价：区间上限上浮 15%，往高了报才有被压价的空间，最终才可能落在目标价附近；
    - 目标价：区间上限（真正想拿到、也合理的价）；
    - 底线价：区间下限，低于它就不划算。

    ⚠️ 返回 **3 个值**（旧版只有 target/bottom）。UI 与解读文案一律读 `suggest_ask`，
    漏接会让「怎么开口」那一段失去数字依据。
    """
    low, high = estimate_salary_range(degree, direction, city_tier,
                                      data=data, experience=experience)
    ask = _round_to_500(high * _ASK_PREMIUM)
    if ask <= high:                      # 兜底：开口价严格高于区间上限
        ask = high + 1000
    return ask, high, low


#: 市场定位四档（对齐 Salary-Negotiation-Skill 的 `get_market_comparison` 口径）。
MARKET_POSITIONS: Dict[str, str] = {
    "below_market": "显著低于市场水平，建议重点争取",
    "low_end":      "处于市场偏低位置，仍有谈判空间",
    "market_rate":  "符合市场水平",
    "above_market": "高于市场水平，建议重点确认非薪资条件",
}


#: 用户输入的月薪里可能带的「单位」写法 → 换算倍数。
#: 只认这几个明确写法，**不猜**其它单位（猜错比不认更糟）。
_OFFER_UNIT_SCALES: Tuple[Tuple[str, int], ...] = (
    ("万", 10000), ("w", 10000), ("W", 10000),
    ("千", 1000), ("k", 1000), ("K", 1000),
)

#: 全角数字 / 逗号 / 句点 → 半角（中文输入法下很容易打出全角）
_FULLWIDTH_MAX = str.maketrans("０１２３４５６７８９，．", "0123456789,.")

#: 夹在数字之间的千分位逗号（``18,000``）—— 必须在取数字**之前**抹掉，
#: 否则正则会先在 ``18`` 处停下，把 18000 读成 18（实测踩过）。
_THOUSANDS_SEP_RE = re.compile(r"(?<=\d),(?=\d)")


def parse_offer_amount(text: Any) -> Optional[int]:
    """把用户在定位框里随手填的月薪解析成整数元；读不出数字返回 ``None``。

    为什么要有它：那是个**自由文本输入框**，用户会写 ``18000`` / ``18,000`` /
    ``18k`` / ``1.8万`` / ``18000元/月``。用户没写对就弹「格式错误」是懒做法；
    这里统一吃掉常见写法，实在读不出来才提示。

    规则（按顺序）：

    1. 全角数字/逗号/句点归一成半角，去掉首尾空白，抹掉千分位逗号；
    2. 命中「万 / w / W」→ ×10000，「千 / k / K」→ ×1000；
    3. 取串里**第一个**数字（可含小数），其余字符（``元`` / ``月`` / ``税前`` /
       逗号）一律忽略；
    4. 结果 ≤ 0、或压根没有数字 → ``None``。

    第 4 条很重要：不能退化成 ``0`` 去喂 ``locate_offer`` —— 那会静默走进
    ``unknown`` 分支，用户以为「算过了」，实际什么都没算。

    已知局限：口语「1万8」「一万八」这种**省略小数点**的写法解析成 10000；
    请写成 ``1.8万``。多个数字（如区间 ``18k-20k``）只取第一个，请填手里那一个。
    """
    if text is None:
        return None
    s = str(text).strip().translate(_FULLWIDTH_MAX)
    s = _THOUSANDS_SEP_RE.sub("", s)
    if not s:
        return None

    scale = 1
    for unit, mult in _OFFER_UNIT_SCALES:
        if unit in s:
            scale = mult
            break

    match = re.search(r"-?\d+(?:\.\d+)?", s)
    if not match:
        return None
    try:
        value = float(match.group()) * scale
    except ValueError:                       # pragma: no cover - 正则已保证可解析
        return None
    if value <= 0:
        return None
    return int(round(value))


def locate_offer(low: int, high: int, offer: int) -> Dict[str, Any]:
    """把一个**具体 offer** 放进市场区间，判断它在哪一档。

    纯函数（不依赖模型 / 网络），供「已经拿到 offer、想知道自己在市场什么位置」
    的场景使用。四档口径对齐 Salary-Negotiation-Skill 的 `get_market_comparison`：

    - `offer < 下限 × 0.9` → below_market
    - `offer < 下限`        → low_end
    - `offer <= 上限`       → market_rate
    - 否则                  → above_market

    `gap_to_high` > 0 表示「离区间上限还有多少可以争取」，是谈判的空间量化。
    数字非法（<=0）时返回 `unknown`，不瞎判。
    """
    try:
        lo, hi, of = int(low), int(high), int(offer)
    except (TypeError, ValueError):
        lo = hi = of = 0
    if lo <= 0 or hi <= 0 or of <= 0:
        return {"position": "unknown", "label": "无法判断（缺少有效数字）",
                "offer": of, "range": [lo, hi], "gap_to_high": 0, "gap_to_high_pct": 0.0}
    if of < lo * 0.9:
        pos = "below_market"
    elif of < lo:
        pos = "low_end"
    elif of <= hi:
        pos = "market_rate"
    else:
        pos = "above_market"
    gap = hi - of
    return {
        "position": pos,
        "label": MARKET_POSITIONS[pos],
        "offer": of,
        "range": [lo, hi],
        "gap_to_high": gap,
        "gap_to_high_pct": round(gap / hi * 100, 1),
    }


def direction_label(direction: str, data: Optional[Dict] = None) -> str:
    """返回方向的中文标签（以当前生效数据为准，缺省用内置常量）。"""
    direction_coef = (data or {}).get("direction_coef") or DIRECTION_COEF
    direction = normalize_direction_key(direction)
    if direction in direction_coef:
        return direction_coef[direction][1]
    return "通用/其他"


def default_data() -> Dict:
    """内置默认基准数据（2026 届调研基线），用于首次落库 seed 与兜底。"""
    return {
        "degree_base": {k: list(v) for k, v in DEGREE_BASE.items()},
        "direction_coef": {k: list(v) for k, v in DIRECTION_COEF.items()},
        "city_tiers": {k: list(v) for k, v in CITY_TIERS.items()},
    }


def unknown_keys(data: Dict) -> Dict[str, List[str]]:
    """列出导入数据里**不在白名单**、会被 `sanitize_data` 丢弃的 key。

    为什么需要它：`sanitize_data` 对不认识的 key 静默用内置默认值兜底
    （这是**有意**的 —— 防止脏数据把整张表算坏），但导入方会看到
    「已导入到 v2」而实际数值一个都没变，属于典型的「提示成功、结果不符预期」。
    这里把被丢弃的 key 交出去，让界面能直说「这些字段名不认识，已忽略」。

    返回形如 `{"degree_base": ["bachelor", "master_985"]}`；无不认识的 key 时返回空 dict。
    """
    if not isinstance(data, dict):
        return {}
    tables = (
        ("degree_base", DEGREE_BASE),
        ("direction_coef", DIRECTION_COEF),
        ("city_tiers", CITY_TIERS),
    )
    out: Dict[str, List[str]] = {}
    for name, default_map in tables:
        raw = data.get(name)
        if not isinstance(raw, dict):
            continue
        unknown = sorted(str(k) for k in raw if k not in default_map)
        if unknown:
            out[name] = unknown
    return out


def _note(report: Optional[Dict], bucket: str, table: str, key: Any) -> None:
    """把一处「被兜底/被丢弃」的字段记进 report（report 为 None 时什么都不做）。"""
    if report is None:
        return
    lst = report.setdefault(bucket, {}).setdefault(table, [])
    name = str(key)
    if name not in lst:
        lst.append(name)


def sanitize_data(data: Dict, report: Optional[Dict] = None) -> Optional[Dict]:
    """校验并清洗外部（手动导入）的基准数据，防止脏数据破坏计算。

    规则（与 Web 版 `salary_benchmark.sanitize_data` 同口径）：
    - `degree_base` 的每个 key 必须是 `[low, high]` 两个正数、且 high >= low；
    - `direction_coef` / `city_tiers` 的每个 key 必须是 `[系数, 标签]`，系数落在 0.3~3.0；
    - 非法或缺失的 key 用内置默认值兜底（保持结构完整，不让一个坏 key 拖垮整表）；
    - 顶层不是 dict、或三个子表都不是 dict 时返回 **None**，由调用方拒绝写入。

    `report`（可选出参）会把「哪些字段其实没按你给的值生效」交出来，分三桶：
    - `replaced_keys`：在白名单里、但**取值非法**，已换成内置默认值；
    - `missing_keys`：在白名单里、但**导入数据没给**，已用内置默认值补齐；
    - `unknown_keys`：**不在白名单**，整条被丢弃。

    为什么需要它：兜底是**有意**的（一个坏 key 不该拖垮整表），但它是**静默**的 ——
    用户看到「已导入到 v2」却发现自己填的数字一个都没变，只能反复重导。
    旧版只报告了 `unknown_keys` 这一种，`replaced_keys` / `missing_keys` 全程无感。
    """
    if not isinstance(data, dict):
        return None

    def _clean_degree_base(raw) -> Optional[Dict[str, List[int]]]:
        if not isinstance(raw, dict):
            return None
        cleaned: Dict[str, List[int]] = {}
        for k, default_v in DEGREE_BASE.items():
            if k not in raw:
                _note(report, "missing_keys", "degree_base", k)
                cleaned[k] = list(default_v)
                continue
            v = raw.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    low, high = float(v[0]), float(v[1])
                except (TypeError, ValueError):
                    return None
                if low > 0 and high >= low:
                    cleaned[k] = [int(round(low)), int(round(high))]
                else:
                    _note(report, "replaced_keys", "degree_base", k)
                    cleaned[k] = list(default_v)
            else:
                _note(report, "replaced_keys", "degree_base", k)
                cleaned[k] = list(default_v)
        for k in raw:
            if k not in DEGREE_BASE:
                _note(report, "unknown_keys", "degree_base", k)
        return cleaned

    def _clean_coef(raw, default_map, table: str) -> Optional[Dict[str, List]]:
        if not isinstance(raw, dict):
            return None
        cleaned: Dict[str, List] = {}
        for k, default_v in default_map.items():
            if k not in raw:
                _note(report, "missing_keys", table, k)
                cleaned[k] = list(default_v)
                continue
            v = raw.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    coef = float(v[0])
                except (TypeError, ValueError):
                    return None
                if 0.3 <= coef <= 3.0:
                    label = str(v[1]) if v[1] else default_v[1]
                    cleaned[k] = [coef, label]
                else:
                    _note(report, "replaced_keys", table, k)
                    cleaned[k] = list(default_v)
            else:
                _note(report, "replaced_keys", table, k)
                cleaned[k] = list(default_v)
        for k in raw:
            if k not in default_map:
                _note(report, "unknown_keys", table, k)
        return cleaned

    degree_base = _clean_degree_base(data.get("degree_base"))
    direction_coef = _clean_coef(data.get("direction_coef"), DIRECTION_COEF, "direction_coef")
    city_tiers = _clean_coef(data.get("city_tiers"), CITY_TIERS, "city_tiers")
    if not degree_base or not direction_coef or not city_tiers:
        return None
    return {
        "degree_base": degree_base,
        "direction_coef": direction_coef,
        "city_tiers": city_tiers,
    }
