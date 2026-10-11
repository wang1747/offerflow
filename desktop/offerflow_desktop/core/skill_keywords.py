"""方向高频技能词表（自服务端 `core/skill_keywords.py` 移植，数据源换成桌面本地真源）。

**服务端的拓扑**：Postgres 的 `resume_skill_keywords` 表（从数百份真实 JD 统计的词频）
→ `top_keywords(direction, n)` → 供 `quality._score_ats` 做「方向高频关键词覆盖」评估。

**桌面的拓扑**：没有那张表，也**不该为此起一个网络依赖**（服务器只当数据库，
但生成要在本机跑通）—— 改用桌面已有的 `services/resume_generation.JOB_TEMPLATES[*]["recommendedSkills"]`
（16 个方向、各 8~14 个技能）作为等价真源。

⚠️ 一份数据两处存放的风险
-------------------------
本文件的 `DIRECTION_SKILLS` 与 `JOB_TEMPLATES` 的 `recommendedSkills` 是同一份内容的两个
副本，由 `tests/test_ai_local_judges.py` 里
`TestSkillKeywords.test_direction_skills_equal_job_templates` **逐字断言两者一致**
（含顺序，漂移即变红）。该套件已登记进 `tests/run_all.py` 发版门槛，反向对照见
`tests/_mutate_ai_judges_check.py` 的 M3。

（本段此前误写为「由 `tests/test_skill_keywords_parity.py` 断言」—— **那个文件从来不存在**，
真正的护栏一直在上面那个位置。2026-10-10 发版 v2.4 复核时更正。）

未改成「统一从本模块取数」的原因：`core` 层**不许 import `services`**（分层约束，参见
`core/ats.py` 的说明），要彻底消除重复只能反向把 `JOB_TEMPLATES[*]["recommendedSkills"]`
下沉到本模块，会动到已上线的生成本地链路 —— 留作 P2，当前靠逐字对照护栏防漂移。

⚠️ 方向值域差异（判据取错键的经典坑）
-------------------------------------
服务端 `direction` 是**中文标签**（如「技术研发」），桌面 `_collect_request()["direction"]`
是**英文 key**（如 ``"tech"``）。若照搬服务端实现直接用 key 查中文表，会**恒返回空** ——
所以 `top_keywords()` 同时接受 key 与 label 两种写法。
"""

from typing import Dict, List, Optional

# 方向 key -> 该方向高频技能（顺序即优先级）
DIRECTION_SKILLS: Dict[str, List[str]] = {
    "tech": ["Python", "Java", "Go", "MySQL", "Redis", "Docker", "Kubernetes",
             "Linux", "Git", "FastAPI", "Spring Boot", "React", "Vue"],
    "ai": ["Python", "PyTorch", "TensorFlow", "scikit-learn", "Transformers", "LangChain", "RAG", "LLM", "Prompt Engineering", "模型微调", "向量数据库", "自然语言处理", "计算机视觉", "机器学习"],
    "product": ["需求分析", "PRD 撰写", "Axure", "Figma", "用户研究", "竞品分析",
                "项目管理", "A/B 测试", "数据分析"],
    "design": ["Figma", "Sketch", "Photoshop", "Illustrator", "设计系统",
               "用户研究", "交互设计", "视觉设计", "After Effects"],
    "operation": ["内容运营", "用户运营", "活动策划", "社群运营", "公众号",
                  "短视频", "文案撰写", "用户增长", "活动复盘"],
    "marketing": ["渠道拓展", "商务谈判", "市场推广", "客户关系", "销售漏斗",
                  "市场调研", "品牌策划", "大客户销售", "CRM"],
    "finance": ["财务分析", "会计", "税务", "审计", "估值建模", "财务报表",
                "成本核算", "CPA", "CFA", "Excel"],
    "legal": ["法律检索", "合同审查", "尽职调查", "法律意见书", "合规", "法考",
              "案例分析", "文书写作"],
    "education": ["教学设计", "课件制作", "教研", "班级管理", "教师资格证",
                  "课程开发", "普通话", "课堂管理"],
    "electrical": ["PLC", "电气设计", "AutoCAD", "EPLAN", "电气原理图", "继电保护",
                   "供配电", "电机控制", "变频器", "单片机", "嵌入式", "MATLAB"],
    "mechanical": ["机械设计", "SolidWorks", "AutoCAD", "UG/NX", "Creo", "ANSYS",
                   "有限元分析", "机械制图", "公差配合", "工艺设计", "CATIA", "CNC"],
    "medical": ["基础护理", "无菌操作", "静脉穿刺", "健康宣教", "病例记录",
                "医患沟通", "急救技能", "护理文书", "生命体征监测"],
    "civil": ["结构设计", "AutoCAD", "PKPM", "YJK", "BIM", "Revit", "施工管理",
              "工程测量", "造价", "广联达", "土木工程材料"],
    "media": ["选题策划", "新闻采写", "视频剪辑", "短视频运营", "公众号运营",
              "采访", "文案", "Premiere", "剪映", "舆情分析"],
    "environmental": ["环境监测", "水处理", "环境影响评价", "环评报告", "水质分析",
                      "AutoCAD", "实验操作", "采样分析", "大气污染防治", "固废处理"],
    "general": ["沟通协调", "团队协作", "项目管理", "时间管理", "问题解决",
                "学习能力", "文档撰写", "办公软件"],
}

# 中文标签 -> 英文 key（桌面 UI 与 JOB_TEMPLATES 用 key，历史数据/服务端用 label）
_LABEL_TO_KEY: Dict[str, str] = {
    "技术研发": "tech",
    "AI/算法/大模型": "ai",
    "产品经理": "product",
    "设计": "design",
    "运营": "operation",
    "市场/销售": "marketing",
    "金融/财会": "finance",
    "法律": "legal",
    "教育/教师": "education",
    "电气/自动化": "electrical",
    "机械/制造": "mechanical",
    "医学护理": "medical",
    "土木/建筑": "civil",
    "新闻/传媒": "media",
    "环境工程": "environmental",
    "通用": "general",
}

# 薪资侧（`core/salary_benchmark`）与简历侧 key 的**同义异名**桥接（C）。
# 同一条目两端曾各起一名：薪资侧 ``environment``（label「环境/环保」）↔
# 简历侧 ``environmental``（label「环境工程」）。这里认薪资侧写法，
# 使 `top_keywords("environment")` 不再恒返回空 —— 与薪资侧
# `salary_benchmark._DIRECTION_KEY_ALIASES` 互为反向。
# 注：薪资侧另有 admin/chip/frontend/logistics 等**简历侧本就无模板**的细分方向，
# 属「薪资 ⊇ 简历」的超集关系，不是漂移，故不在此硬凑。
_KEY_ALIASES: Dict[str, str] = {
    "environment": "environmental",
}


def _resolve_direction(direction: Optional[str]) -> Optional[str]:
    """把 key / label / 大小写差异统一成 key；无法识别返回 None。"""
    if not direction:
        return None
    d = str(direction).strip()
    if d in DIRECTION_SKILLS:
        return d
    if d.lower() in _KEY_ALIASES:
        return _KEY_ALIASES[d.lower()]
    if d in _LABEL_TO_KEY:
        return _LABEL_TO_KEY[d]
    low = d.lower()
    for key in DIRECTION_SKILLS:
        if key.lower() == low:
            return key
    return None


def top_keywords(direction: str, n: int = 12) -> List[str]:
    """某岗位方向的 top N 技能关键词（顺序即优先级）。

    与服务的不同：服务端按真实 JD 词频降序取前 N，桌面按人工整理的优先级取前 N。
    方向无法识别时返回空 —— 与服务端「表里没有该方向」的行为一致（调用方会跳过）。
    """
    key = _resolve_direction(direction)
    if not key:
        return []
    return DIRECTION_SKILLS[key][:n]


def directions() -> List[str]:
    """所有已收录的岗位方向 key。"""
    return list(DIRECTION_SKILLS.keys())


def direction_label(key_or_label: str) -> str:
    """把 key 或 label 归一成中文标签（UI 展示用）。"""
    key = _resolve_direction(key_or_label)
    if not key:
        return ""
    for label, k in _LABEL_TO_KEY.items():
        if k == key:
            return label
    return ""
