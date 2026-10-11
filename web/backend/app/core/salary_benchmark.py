"""应届生市场薪资基准表。

核心原则：**薪资数字由真实市场数据 + 规则计算，不由 LLM 编造**。
LLM 只负责从简历提取「学历 / 学校层次 / 求职方向 / 城市」，本模块据此查表算区间。

基准数据存放在同目录 `salary_benchmark_data.json`（可在宿主机挂载、直接编辑更新，
无需改代码或重建镜像），本模块优先从该文件加载；文件缺失/损坏时回退到内置默认值。

所有区间为**税前月薪（元）**，不含年终奖、绩效、补贴、股票。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============ 内置兜底数据（JSON 缺失/损坏时使用） ============
_FALLBACK: dict = {
    "version": "2026-08",
    "updated_at": "2026-08-30",
    "description": "2026 届应届生起薪市场基准（税前月薪，单位：元），不含年终奖、绩效、补贴、股票。",
    "sources": [
        "智联猎头《2026 年度企业薪酬调研报告》（应届生起薪，分学历/院校/城市）",
        "福睿思特《2026 年 IT 互联网/软件开发行业薪资水平报告》（分岗位/城市）",
        "麦可思《2026 年中国本科生就业蓝皮书》（分城市月收入）",
        "新东方《2026 届校招薪资调查报告》（28.6 万样本，分行业/学历/城市）",
    ],
    "degree_base": {
        "associate": [4000, 6000],
        "bachelor_normal": [6000, 9000],
        "bachelor_elite": [8000, 13000],
        "master": [10000, 16000],
        "phd": [22000, 30000],
    },
    "degree_labels": {
        "associate": "大专",
        "bachelor_normal": "本科（普通院校）",
        "bachelor_elite": "本科（985/211）",
        "master": "硕士",
        "phd": "博士",
    },
    "direction_coef": {
        "ai": [1.70, "AI/算法/大模型"],
        "frontend": [1.25, "前端开发"],
        "tech": [1.35, "技术研发"],
        "chip": [1.45, "半导体/芯片/硬件"],
        "product": [1.05, "产品经理"],
        "design": [0.95, "设计"],
        "operation": [0.90, "运营/新媒体"],
        "marketing": [0.90, "市场/销售"],
        "finance": [1.05, "金融/财会"],
        "legal": [0.95, "法律/法务"],
        "admin": [0.85, "行政/人事/文职"],
        "education": [0.85, "教育/教师"],
        "medical": [0.95, "医疗/护理/医药"],
        "mechanical": [0.95, "机械/制造"],
        "electrical": [1.00, "电气/自动化"],
        "civil": [0.90, "土木/建筑"],
        "media": [0.85, "传媒/新闻"],
        "environment": [0.85, "环境/环保"],
        "logistics": [0.85, "物流/采购/供应链"],
        "general": [0.90, "通用/其他"],
    },
    "city_tiers": {
        "tier1": [1.00, "一线城市（北上广深）"],
        "tier2": [0.85, "新一线 / 强二线（杭州、成都、南京、武汉、苏州等）"],
        "tier3": [0.72, "二三线城市"],
    },
}


def _load_benchmark_data() -> dict:
    """从同目录 JSON 加载基准数据；缺失/损坏返回 None（回退内置）。"""
    try:
        path = Path(__file__).parent / "salary_benchmark_data.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("degree_base", "direction_coef", "city_tiers", "degree_labels", "sources"):
            if key not in data:
                return None
        return data
    except Exception:
        return None


_DATA = _load_benchmark_data() or _FALLBACK

# ============ 模块级常量（优先 JSON，兜底内置） ============
DATA_VERSION: str = str(_DATA.get("version", "2026-08"))
DATA_UPDATED_AT: str = str(_DATA.get("updated_at", "2026-08-30"))
DATA_DESCRIPTION: str = str(_DATA.get("description", ""))
DATA_SOURCES: List[str] = list(_DATA.get("sources", []))
DEGREE_BASE: Dict[str, Tuple[int, int]] = {
    k: tuple(v) for k, v in _DATA["degree_base"].items()
}
DEGREE_LABELS: Dict[str, str] = dict(_DATA["degree_labels"])
DIRECTION_COEF: Dict[str, Tuple[float, str]] = {
    k: tuple(v) for k, v in _DATA["direction_coef"].items()
}
CITY_TIERS: Dict[str, Tuple[float, str]] = {
    k: tuple(v) for k, v in _DATA["city_tiers"].items()
}


def get_data_version() -> dict:
    """返回数据版本信息，供前端展示数据新鲜度。"""
    return {
        "version": DATA_VERSION,
        "updated_at": DATA_UPDATED_AT,
        "description": DATA_DESCRIPTION,
    }

_TIER1_CITIES = {"北京", "上海", "深圳", "广州"}
_TIER2_CITIES = {
    "杭州", "成都", "南京", "武汉", "苏州", "西安", "重庆", "天津",
    "长沙", "郑州", "青岛", "合肥", "东莞", "佛山", "宁波", "无锡",
    "厦门", "福州", "济南", "大连", "沈阳", "昆明", "南昌", "哈尔滨",
}

# ============ 方向归一化关键词 ============
# 用于把 LLM 提取到的岗位名/方向，或用户手填的岗位，映射到标准 direction key。

# 标准标签 -> key（由 DIRECTION_COEF 的 label 反向生成，标签改了这里自动同步）
_DIRECTION_LABEL_MAP: Dict[str, str] = {
    label: key for key, (_, label) in DIRECTION_COEF.items()
}

_DIRECTION_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("ai", ["ai", "算法", "大模型", "机器学习", "深度学习", "自然语言", "nlp",
            "计算机视觉", "数据科学", "推荐算法", "智能", "人工智能", "cv", "llm"]),
    ("chip", ["芯片", "半导体", "集成电路", "ic", "硬件", "fpga", "微电子", "嵌入式硬件"]),
    ("frontend", ["前端", "web前端", "大前端", "h5", "web", "小程序"]),
    ("tech", ["技术研发", "研发", "技术", "后端", "开发", "工程师", "java", "python", "go", "golang",
              "c++", "测试", "运维", "大数据", "数据分析", "数据库", "安卓", "android",
              "ios", "客户端", "全栈", "程序员", "软件", "云计算", "sre", "devops"]),
    ("product", ["产品", "pm", "需求分析"]),
    ("design", ["设计", "ui", "ux", "视觉", "交互", "平面", "美工", "原画"]),
    ("operation", ["运营", "新媒体", "内容", "社群", "用户增长", "电商运营"]),
    ("marketing", ["市场", "销售", "商务", "bd", "客户经理", "渠道", "品牌", "营销", "推广"]),
    ("finance", ["金融", "财会", "会计", "财务", "审计", "银行", "证券", "基金",
                 "投资", "风控", "税务", "出纳", "精算"]),
    ("legal", ["法律", "法务", "律师", "合规", "知识产权"]),
    ("admin", ["行政", "人事", "hr", "人力", "文员", "文秘", "文职", "助理", "前台",
               "档案", "考勤", "招聘专员"]),
    ("education", ["教师", "老师", "教育", "培训", "讲师", "课程", "教研", "幼师"]),
    ("medical", ["医疗", "护理", "护士", "医药", "临床", "药剂", "医学", "健康管理"]),
    ("mechanical", ["机械", "制造", "工艺", "结构", "模具", "装配", "机加工", "cad制图"]),
    ("electrical", ["电气", "自动化", "电力", "plc", "继电", "供配电", "变频", "单片机"]),
    ("civil", ["土木", "建筑", "工程", "造价", "结构", "施工", "监理", "勘察", "测绘", "bim"]),
    ("media", ["传媒", "新闻", "编辑", "记者", "影视", "编导", "摄影", "文案", "主持", "播音"]),
    ("environment", ["环境", "环保", "环评", "水处理", "固废", "废气", "生态", "监测"]),
    ("logistics", ["物流", "采购", "供应链", "仓储", "运输", "快递", "库存"]),
]

# ============ 学历 / 学校层次归一化关键词 ============
_ELITE_SCHOOL_KEYWORDS = ["985", "211", "双一流", "c9", "清北", "top2"]
_ASSOCIATE_KEYWORDS = ["大专", "专科", "高职", "职业技术学院", "专科学校"]


def _round_to_500(value: float) -> int:
    """四舍五入到 500 元档，让数字看起来更像真实的定薪区间。"""
    return int(round(value / 500.0)) * 500


def resolve_degree(raw: str) -> str:
    """把 LLM / 用户输入的学历描述归一化到标准 key。"""
    text = (raw or "").strip()
    if any(k in text for k in ["博士", "phd", "博士后"]):
        return "phd"
    if any(k in text for k in ["硕士", "研究生", "master"]):
        return "master"
    if any(k in text for k in _ASSOCIATE_KEYWORDS) or text in ("大专", "专科"):
        return "associate"
    # 本科：区分 985/211 与普通
    if any(k in text for k in _ELITE_SCHOOL_KEYWORDS):
        return "bachelor_elite"
    # 默认本科普通（含"本科""学士""大学"等）
    return "bachelor_normal"


def resolve_school_tier(raw: str) -> str:
    """学校层次：985/211 -> elite，专科 -> associate，否则 normal。"""
    text = (raw or "").strip()
    if any(k in text for k in _ELITE_SCHOOL_KEYWORDS):
        return "elite"
    if any(k in text for k in _ASSOCIATE_KEYWORDS):
        return "associate"
    return "normal"


def resolve_direction(raw: str) -> str:
    """把岗位名 / 求职方向描述归一化到标准 direction key。"""
    text = (raw or "").strip()
    if not text:
        return "general"
    # 1. 标准标签精确匹配（LLM 按 prompt 选项返回标准标签时直接命中）
    for label, key in _DIRECTION_LABEL_MAP.items():
        if label in text:
            return key
    # 2. 关键词匹配（岗位名 / 用户手填的自由文本）
    lowered = text.lower()
    for key, keywords in _DIRECTION_KEYWORDS:
        for kw in keywords:
            if kw in lowered:
                return key
    return "general"


def resolve_city_tier(city: str) -> str:
    """城市 -> 城市档。"""
    c = (city or "").strip()
    if not c:
        return "tier1"
    # 去掉常见后缀
    for suffix in ("市", "省"):
        if c.endswith(suffix):
            c = c[:-1]
    if any(name in c for name in _TIER1_CITIES):
        return "tier1"
    if any(name in c for name in _TIER2_CITIES):
        return "tier2"
    return "tier3"


# ============ 经验年限系数 ============
# 基准表是「应届生起薪」，社招（有工作经验）在此基础上按年限上浮。
# 用代码层规则而非数据表，避免与版本化基准数据耦合。
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


def estimate_salary_range(
    degree: str,
    direction: str,
    city_tier: str,
    data: Optional[Dict] = None,
    experience: float = 0.0,
) -> Tuple[int, int]:
    """根据学历、岗位方向、城市档、经验年限计算税前月薪区间 [low, high]。

    `data` 可选：传入落库的生效基准数据（{"degree_base":..., "direction_coef":..., "city_tiers":...}），
    缺省时回退到内置常量（2026 届调研基线）。
    `experience`：工作年限（年），>0 时在应届起薪基础上按经验系数上浮。
    """
    degree_base = (data or {}).get("degree_base") or DEGREE_BASE
    direction_coef = (data or {}).get("direction_coef") or DIRECTION_COEF
    city_tiers = (data or {}).get("city_tiers") or CITY_TIERS

    degree = degree if degree in degree_base else "bachelor_normal"
    direction = direction if direction in direction_coef else "general"
    city_tier = city_tier if city_tier in city_tiers else "tier1"

    base_low, base_high = degree_base[degree]
    coef = direction_coef[direction][0]
    city_coef = city_tiers[city_tier][0]
    exp_coef = experience_coef(experience)

    low = _round_to_500(base_low * coef * city_coef * exp_coef)
    high = _round_to_500(base_high * coef * city_coef * exp_coef)
    # 保证下限 < 上限，且不低于法定最低工资参考（约 2300）
    if high <= low:
        high = low + 500
    return low, high


# 开口价上浮系数：谈判锚定策略——开口价要高于市场价上限，给 HR 压价留出空间，
# 否则开口价=上限，HR 一压就跌破你的真实期望。
_ASK_PREMIUM = 1.15


def suggest_target_and_bottom(
    degree: str,
    direction: str,
    city_tier: str,
    data: Optional[Dict] = None,
    experience: float = 0.0,
) -> Tuple[int, int, int]:
    """给出谈判建议：开口价（报价锚点）、目标价（期望落地）、底线价。

    - 开口价：区间上限上浮 15%，往高了报才有被 HR 压价的空间，最终才可能落在目标价附近；
    - 目标价：区间上限（你真正想拿到、也合理的价）；
    - 底线价：区间下限，低于它就不划算。
    """
    low, high = estimate_salary_range(degree, direction, city_tier, data=data, experience=experience)
    ask = _round_to_500(high * _ASK_PREMIUM)
    if ask <= high:  # 兜底：确保开口价严格高于区间上限
        ask = high + 1000
    return ask, high, low


def direction_label(direction: str, data: Optional[Dict] = None) -> str:
    """返回方向的中文标签（以当前生效数据为准）。"""
    direction_coef = (data or {}).get("direction_coef") or DIRECTION_COEF
    if direction in direction_coef:
        return direction_coef[direction][1]
    return "通用/其他"


def default_data() -> Dict:
    """返回内置默认基准数据（2026 届调研基线），用于首次落库 seed 与兜底。"""
    return {
        "degree_base": DEGREE_BASE,
        "direction_coef": DIRECTION_COEF,
        "city_tiers": CITY_TIERS,
    }


def sanitize_data(data: Dict) -> Dict:
    """校验并清洗外部（LLM 生成 / 手动导入）的基准数据，防止脏数据破坏计算。

    规则：
    - degree_base 的每个 key 必须是 [low, high] 两个正数；
    - direction_coef 的每个 key 必须是 [系数, 标签]；
    - city_tiers 的每个 key 必须是 [系数, 标签]；
    - 清洗后不完整则返回 None 由调用方回退默认数据。
    """
    if not isinstance(data, dict):
        return None

    def _clean_degree_base(raw):
        cleaned = {}
        if not isinstance(raw, dict):
            return None
        for k in DEGREE_BASE:
            v = raw.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    low, high = float(v[0]), float(v[1])
                except (TypeError, ValueError):
                    return None
                if low > 0 and high >= low:
                    cleaned[k] = [int(round(low)), int(round(high))]
            else:
                # 缺 key 时用默认值兜底，保证结构完整
                cleaned[k] = list(DEGREE_BASE[k])
        return cleaned

    def _clean_coef(raw, default_map):
        cleaned = {}
        if not isinstance(raw, dict):
            return None
        for k in default_map:
            v = raw.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    coef = float(v[0])
                except (TypeError, ValueError):
                    return None
                if 0.3 <= coef <= 3.0:
                    label = str(v[1]) if v[1] else default_map[k][1]
                    cleaned[k] = [coef, label]
                else:
                    cleaned[k] = list(default_map[k])
            else:
                cleaned[k] = list(default_map[k])
        return cleaned

    degree_base = _clean_degree_base(data.get("degree_base"))
    direction_coef = _clean_coef(data.get("direction_coef"), DIRECTION_COEF)
    city_tiers = _clean_coef(data.get("city_tiers"), CITY_TIERS)

    if not degree_base or not direction_coef or not city_tiers:
        return None

    return {
        "degree_base": degree_base,
        "direction_coef": direction_coef,
        "city_tiers": city_tiers,
    }
