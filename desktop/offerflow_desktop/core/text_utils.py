"""通用文本工具：技能词典与识别（自 Web 版移植，去除 DB 词频依赖）。"""

import re
from typing import Dict, List, Set

# 技能词典：按「求职方向」分组，与 services/resume_generation.py 的
# JOB_TEMPLATES（16 个方向）对齐——否则非技术岗的 ATS 命中率与岗位匹配度
# 会整体塌成 0（词典里根本没有机械/财会/法律/护理/传媒的词）。
#
# 约定：
# * 纯 ASCII 别名按词边界匹配（见 alias_in_text），不要放 1~2 个字母的缩写
#   （``tf`` 会被 TF-IDF 命中、``ps``/``pr``/``rn`` 同理）；
# * 中文别名用子串匹配，**至少 2 个字**，避免「安全帽」命中「安全」这类噪声。
SKILL_ALIASES: Dict[str, List[str]] = {
    # ---------- 编程语言 / 基础 ----------
    "Python": ["python", "python3", "py"],
    "Java": ["java"],
    "Go": ["go", "golang"],
    "C++": ["c++", "cpp"],
    "C#": ["c#"],
    "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript", "ts"],
    "HTML/CSS": ["html", "css", "html5", "css3"],
    "Shell": ["shell", "bash"],

    # ---------- 后端 / 中间件 ----------
    "FastAPI": ["fastapi"],
    "Flask": ["flask"],
    "Django": ["django"],
    "Spring Boot": ["spring boot", "spring"],
    "Node.js": ["node", "nodejs", "node.js"],
    "RESTful API": ["restful", "rest api"],
    "gRPC": ["grpc"],
    "MySQL": ["mysql"],
    "PostgreSQL": ["postgresql", "postgres"],
    "SQL": ["sql", "sqlite", "mysql", "postgresql", "postgres", "oracle",
            "sql server", "sqlserver", "mariadb"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Elasticsearch": ["elasticsearch"],
    "Kafka": ["kafka"],
    "RabbitMQ": ["rabbitmq", "rocketmq"],
    "Celery": ["celery"],
    "Nginx": ["nginx"],
    "消息队列": ["消息队列", "message queue"],
    "微服务": ["微服务", "microservice", "microservices"],
    "高并发": ["高并发", "high concurrency"],
    "分布式": ["分布式", "distributed"],
    "CI/CD": ["ci/cd", "cicd", "jenkins"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Linux": ["linux", "unix"],
    "Git": ["git", "github", "gitlab"],
    "AWS": ["aws", "亚马逊云"],
    "阿里云": ["阿里云", "aliyun"],
    "腾讯云": ["腾讯云", "tencentcloud"],

    # ---------- 前端 / 移动 ----------
    "React": ["react"],
    "Vue": ["vue", "vuejs"],
    "Next.js": ["next.js", "nextjs"],
    "Webpack": ["webpack", "vite"],
    "Tailwind": ["tailwind", "tailwindcss"],
    "数据可视化": ["数据可视化", "echarts", "recharts", "power bi", "tableau", "matplotlib"],
    "小程序": ["小程序", "miniprogram"],
    "Android": ["android", "安卓"],
    "iOS": ["ios", "swift"],
    "Flutter": ["flutter", "dart"],
    "性能优化": ["性能优化", "性能调优", "调优", "压测"],

    # ---------- AI / 大模型 / 数据 ----------
    "RAG": ["rag", "检索增强", "检索增强生成"],
    "LLM": ["llm", "大模型", "大语言模型"],
    "Prompt Engineering": ["prompt", "prompt engineering", "提示词"],
    "向量数据库": ["向量数据库", "chromadb", "chroma", "faiss", "milvus",
                   "pgvector", "pinecone"],
    "LangChain": ["langchain", "langgraph"],
    "Agent": ["agent", "智能体"],
    "Embedding": ["embedding", "向量化", "bge"],
    "BM25": ["bm25"],
    "模型微调": ["微调", "fine-tune", "fine tune", "finetune", "lora", "sft"],
    "PyTorch": ["pytorch", "torch"],
    "TensorFlow": ["tensorflow"],
    "scikit-learn": ["scikit learn", "scikit-learn", "sklearn"],
    "自然语言处理": ["nlp", "自然语言处理"],
    "计算机视觉": ["计算机视觉", "opencv"],
    "机器学习": ["机器学习", "machine learning"],
    "深度学习": ["深度学习", "deep learning"],
    "算法": ["算法", "algorithm", "leetcode"],
    "数据分析": ["数据分析", "data analysis"],
    "SQL 优化": ["索引优化", "慢查询", "sql 优化"],
    "测试": ["测试", "pytest", "单元测试", "unit test", "自动化测试"],
    "安全": ["安全", "security"],
    "爬虫": ["爬虫", "scrapy", "selenium"],

    # ---------- 产品 / 设计 ----------
    "需求分析": ["需求分析", "需求调研", "prd", "需求文档"],
    "原型设计": ["原型", "原型设计", "axure"],
    "竞品分析": ["竞品分析", "竞品调研"],
    "用户研究": ["用户研究", "用户调研", "用户访谈"],
    "A/B 测试": ["a/b 测试", "ab 测试", "abtest"],
    "项目管理": ["项目管理", "排期", "进度管理"],
    "Figma": ["figma"],
    "Sketch": ["sketch"],
    "Photoshop": ["photoshop"],
    "Illustrator": ["illustrator"],
    "After Effects": ["after effects"],
    "Premiere": ["premiere"],
    "设计系统": ["设计系统", "组件库", "设计规范"],
    "交互设计": ["交互设计", "交互"],
    "视觉设计": ["视觉设计", "视觉"],

    # ---------- 运营 / 市场 / 传媒 ----------
    "内容运营": ["内容运营", "选题", "文案", "公众号", "小红书", "短视频", "直播"],
    "用户运营": ["用户运营", "用户分层", "留存", "拉新", "用户增长"],
    "社群运营": ["社群", "社群运营"],
    "活动策划": ["活动策划", "活动运营"],
    "新媒体": ["新媒体", "媒体运营"],
    "视频剪辑": ["剪辑", "剪映", "pr 剪辑"],
    "新闻采写": ["采访", "采写", "新闻稿", "稿件"],
    "舆情分析": ["舆情"],
    "SEM/SEO": ["sem", "seo", "投放"],
    "商务谈判": ["商务谈判", "谈判"],
    "渠道拓展": ["渠道拓展", "渠道"],
    "CRM": ["crm"],
    "市场调研": ["市场调研", "市场分析"],

    # ---------- 金融 / 财会 ----------
    "财务分析": ["财务分析", "财报分析", "经营分析", "财务模型"],
    "财务报表": ["财务报表", "资产负债表", "利润表", "现金流量表"],
    "成本核算": ["成本核算", "成本会计", "成本管理"],
    "审计": ["审计", "底稿", "函证"],
    "税务": ["税务", "税法", "纳税申报"],
    "会计": ["会计", "记账", "凭证", "总账", "核算"],
    "估值建模": ["估值", "dcf", "可比公司"],
    "Excel": ["excel", "vlookup", "数据透视表"],
    "CPA": ["cpa", "注会"],
    "CFA": ["cfa"],
    "用友/金蝶": ["用友", "金蝶", "sap"],

    # ---------- 法律 ----------
    "法律检索": ["法律检索", "案例检索", "北大法宝", "威科先行"],
    "合同审查": ["合同审查", "合同审核", "审合同", "合同起草"],
    "法律意见书": ["法律意见书", "律师函", "法律文书"],
    "尽职调查": ["尽职调查", "尽调"],
    "诉讼": ["诉讼", "仲裁", "出庭"],
    "合规": ["合规", "个人信息保护", "数据合规"],
    "公司法务": ["公司法", "劳动关系", "劳动争议"],

    # ---------- 机械 / 电气 / 自动化 ----------
    "SolidWorks": ["solidworks"],
    "AutoCAD": ["autocad", "cad"],
    "UG/NX": ["ug/nx", "ug nx", "unigraphics"],
    "CATIA": ["catia"],
    "Creo": ["creo", "pro/e", "proe"],
    "ANSYS": ["ansys", "有限元", "有限元分析", "仿真分析"],
    "机械设计": ["机械设计", "结构设计", "机械结构", "非标"],
    "机械制图": ["机械制图", "工程图", "出图", "图纸"],
    "公差配合": ["公差", "形位公差"],
    "工艺设计": ["加工工艺", "工艺流程", "工艺设计", "装配工艺"],
    "CNC": ["cnc", "数控"],
    "PLC": ["plc", "梯形图", "西门子", "三菱"],
    "单片机": ["单片机", "stm32", "嵌入式"],
    "电气设计": ["电气设计", "电气原理图", "供配电", "继电保护"],
    "MATLAB": ["matlab", "simulink"],

    # ---------- 土木 / 建筑 / 环境 ----------
    "PKPM": ["pkpm"],
    "YJK": ["yjk", "盈建科"],
    "BIM": ["bim", "revit"],
    "造价": ["造价", "广联达", "工程量"],
    "施工管理": ["施工管理", "现场管理", "施工组织"],
    "工程测量": ["全站仪", "水准仪", "工程测量"],
    "环境监测": ["环境监测", "水质监测", "采样"],
    "环境影响评价": ["环境影响评价", "环评"],
    "水处理": ["水处理", "污水处理", "废水"],

    # ---------- 医学 / 护理 ----------
    "基础护理": ["基础护理", "护理操作"],
    "静脉穿刺": ["静脉穿刺", "输液"],
    "无菌操作": ["无菌操作", "无菌"],
    "生命体征监测": ["生命体征", "心电监护", "监测血压"],
    "急救技能": ["急救", "心肺复苏", "cpr"],
    "护理文书": ["护理文书", "护理记录"],
    "健康宣教": ["健康宣教", "健康教育"],
    "医患沟通": ["医患沟通", "患者沟通"],
    "病例书写": ["病历", "病例记录"],
    "临床轮转": ["轮转", "临床实习"],

    # ---------- 教育 ----------
    "教学设计": ["教学设计", "教案", "备课"],
    "课件制作": ["课件", "课件制作"],
    "班级管理": ["班级管理", "班主任"],
    "教研": ["教研", "磨课", "集体备课"],
    "教师资格证": ["教师资格证"],
    "课程开发": ["课程开发", "校本课程"],

    # ---------- 通用素养 ----------
    "沟通协调": ["沟通", "协调", "对接"],
    "团队协作": ["团队协作", "团队合作", "协作"],
    "办公软件": ["office", "wps", "ppt", "word"],
    "文档撰写": ["文档", "技术文档", "文档撰写", "白皮书"],
    "英语能力": ["cet-4", "cet-6", "cet4", "cet6", "英语六级", "英语四级",
                 "雅思", "托福", "专四", "专八"],
    "演讲表达": ["演讲", "答辩", "汇报", "宣讲"],
}


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[_\-\s]+", " ", text)
    return text


# 纯 ASCII 别名（含技术符号），可以按「词边界」匹配
# 待填数字占位符：与 prompts._PLACEHOLDER_RULE 的约定一致 ——
# 模型在「需要量化但原文没有数字」时写 [x]，绝不编造；这里负责把它们找出来
# 提醒用户补填（否则占位符会被直接导出到成品里）。
_PLACEHOLDER_RE = re.compile(r"\[x\]", re.IGNORECASE)


def count_placeholders(text: str) -> int:
    """文本里还有几处未填的 [x] 占位符。"""
    return len(_PLACEHOLDER_RE.findall(text or ""))


def find_placeholders(text: str) -> List[str]:
    """列出未填的 [x] 占位符（各带一小段上下文，便于用户定位）。

    同一片段去重；不含占位符时返回空列表。
    """
    if not text:
        return []
    out: List[str] = []
    for m in _PLACEHOLDER_RE.finditer(text):
        frag = text[max(0, m.start() - 12):m.end() + 12].replace("\n", " ").strip()
        if frag and frag not in out:
            out.append(frag)
    return out


_ASCII_ALIAS_RE = re.compile(r"^[a-z0-9+#./ -]+$")


def alias_in_text(alias: str, text_norm: str) -> bool:
    """别名是否出现在已归一化的文本里（**大小写不敏感**）。

    ASCII 别名按**词边界**匹配，修掉「子串巧合」造成的假命中：
    ``java`` 不再命中 ``javascript``，``go`` 不再命中 ``google``，
    ``sql`` 不再命中 ``postgresql``。
    CJK 别名仍用子串匹配（中文没有词边界）。

    ⚠️ 2026-10-11 修：别名先 ``lower()`` 再匹配。此前把**展示名**
    （``Python`` / ``PyTorch`` / ``RAG`` …）直接丢进来的调用方
    （如 ``quality._score_ats`` 的「方向高频词覆盖」）会**恒判为未命中** ——
    ``text_norm`` 已小写、而别名未小写，词边界/子串都对不上，表现为
    「技能明明写在简历里，却被报「缺失」」。别名本身若已小写
    （``SKILL_ALIASES`` 的约定），本函数行为完全不变。
    """
    alias = (alias or "").strip().lower()
    if not alias or not text_norm:
        return False
    if _ASCII_ALIAS_RE.match(alias):
        pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
        return re.search(pattern, text_norm) is not None
    return alias in text_norm


def extract_skills(text: str) -> Set[str]:
    """从文本中识别已知技能（词典 + 别名匹配，ASCII 按词边界）。"""
    normalized = normalize_text(text)
    found: Set[str] = set()
    for skill, aliases in SKILL_ALIASES.items():
        if any(alias_in_text(alias, normalized) for alias in aliases):
            found.add(skill)
    return found
