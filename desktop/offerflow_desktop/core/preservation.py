"""原始简历分块与内容保留校验（自 Web 版 `modules/resume/optimizer/sections.py` 移植）。

均为纯函数，不依赖 LLM、数据库或网络。

整份重写场景下，「保留校验」的核心目标是：关键事实（联系方式、时间、数字、
英文技术词、专有名词）**不丢失、不新增**，而不是逐字保留原文。

与 Web 版的对应关系（2026-10-10 对齐）：

    Web `optimizer/sections.py`          → 本文件
    ------------------------------------------------
    `_MIN_TERM_RETENTION = 0.35`         → 同（原先本地误用 0.60，见下）
    `_extract_added_facts`               → 同（本地此前**完全没有**）
    `_GLUED_CONTACT` / 粘连拆分          → 同
    `split_resume_blocks`                → 同
    `block_preserved`                    → 同

⚠️ 阈值为什么必须是 0.35：整份简历的中文改写会自然替换大量相邻词（「负责开发」
→「主导搭建」），二元组重合度天然掉到 0.6 以下。0.60 会把正常的**同义改写**
误判为「丢失内容」而回退原文 —— 表现是「点了优化，结果和原文一模一样」。
0.35 仍能拦住整段替换，但不再要求逐句近似。

⚠️ 为什么必须查 `added_facts`：只查「丢了什么」是不够的。优化稿新增一个原文
没有的公司名 / 数字 / 职位，属于**编造**，后果比丢内容严重得多（面试被追问
就穿帮）。这条此前本地缺失，等于「编造照样落库」。
"""

import re
from typing import Dict, List, Set

SECTION_HEADER = re.compile(
    r"^\s*(?:教育|教育背景|教育经历|项目|项目经历|实习|实习经历|工作|工作经历|"
    r"经历|技能|专业技能|自我评价|个人评价|荣誉|获奖|证书|个人信息|基本资料|求职意向|"
    r"个人总结|个人简介|专业技能|掌握技能)"
    r"[^：:]{0,12}[:：]?\s*$"
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
# 手机号（11 位）或座机（区号 2-4 位 + 分隔符 + 号码 7-8 位），
# 避免把「2023-2027」这类年份区间误判为电话
_PHONE = re.compile(r"(?:1[3-9]\d{9})|(?:\+?\d{2,4}[\s-]\d{7,8})")
# URL 只匹配到空白/中文/中文标点为止，避免把后面紧跟的中文（如「个人信息」）粘连进来
_URL = re.compile(r"https?://[^\s\u4e00-\u9fff，。；：、（）【】《》]+")
# 明显年份：四位数字后跟「年/届/级/月」（如 2027届、2023年），
# 避免把「处理 2000 并发」这类量化数字误判为年份
_YEAR = re.compile(r"(?:19|20)\d{2}(?=\s*(?:年|届|级|月))")
# 英文技术词 / 专有名词（含大写开头、点、井号、加号、斜杠）
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{1,}")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
# 「新增数字」的识别范围：只抓**明显是成果/规模**的数字，不抓「3年」这类
# 可能被措辞改写成「三年」的普通数字（那属于合理表达调整）。
_NEW_FACT_NUMBER = re.compile(
    r"(?<!\d)(?:\d+(?:\.\d+)?%|\d{2,}(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s*(?:倍|万|亿|年|个月|月|周|天|小时|分钟|秒|毫秒|ms|"
    r"次|条|项|家|人|位|名|份|组|台|户|笔|张|套))(?!\d)",
    re.IGNORECASE,
)
# 「角色头衔」：原文没有、改写稿突然冒出来的职级，是典型的编造信号
_ROLE_TERMS = (
    "架构师", "项目负责人", "技术负责人", "负责人", "主导人", "创始人",
    "联合创始人", "主管", "总监", "经理", "首席",
)
# 「曾任/任职于 X」这类明确的任职关系，X 原文没有 → 编造
_ORG_CONTEXT = re.compile(
    r"(?:曾任|任职于|就职于|曾在|入职|加入)"
    r"([\u4e00-\u9fffA-Za-z0-9]{2,24}?)"
    r"(?=架构师|工程师|经理|主管|总监|负责人|开发|实习|[,，。；;\s]|$)"
)
# 跨文本框/WPS 节点粘连：`xx@qq.comGitHub：...` 会被 `_URL` 当成一个整体 URL。
# 在「联系人」这种位置插一个零宽断言，把粘连点拆开。
_GLUED_CONTACT = re.compile(
    r"(?i)(?<=[a-z0-9])(?=(?:"
    r"github|linkedin|gitee|gitlab|leetcode|nowcoder|zhihu|bilibili|"
    r"weibo|twitter|https?://|www\.))"
)

# 整份简历中文改写会自然替换大量相邻词，0.60 会把「同义改写」误判为丢内容；
# 0.35 仍能拦住整段替换，但不再要求逐句近似。
_MIN_TERM_RETENTION = 0.35


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _separate_glued_contacts(text: str) -> str:
    """拆开 `邮箱@domainGitHub：...` 这类跨文本框/WPS 节点粘连。"""
    return _GLUED_CONTACT.sub(" ", text or "")


def split_resume_blocks(text: str) -> List[str]:
    """按常见简历小节标题切块，未识别到标题时按行切块。

    导入/粘贴进来的简历只有一整段文本，逐段处理（如逐段重写、逐段质检）
    都先要一个稳定的分块口径 —— 这里是那一个口径。
    """
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return []

    blocks: List[str] = []
    current: List[str] = []
    for line in lines:
        if SECTION_HEADER.match(line) and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _extract_critical_facts(text: str) -> Set[str]:
    """提取「绝对不能丢」的硬事实：联系方式、链接、四位年份。

    这些表述稳定，不会因措辞优化而改变，丢了即视为篡改。注意：普通数字
    （如「3年」「500ms」）不在此列 —— LLM 整份重写时可能把「3年」改写成
    「三年」，属于合理措辞调整，不应硬拦截。
    """
    text = _separate_glued_contacts(text or "")
    critical: Set[str] = set()
    for pattern in (_EMAIL, _PHONE, _URL, _YEAR):
        critical.update(_normalize(item) for item in pattern.findall(text or "") if item)
    return critical


def _extract_critical_categories(text: str) -> Dict[str, int]:
    """按类别统计硬事实数量（邮箱/电话/链接/时间），用于前端「事实保真」可视化。"""
    text = _separate_glued_contacts(text or "")
    categories = {
        "邮箱": len(_EMAIL.findall(text or "")),
        "电话": len(_PHONE.findall(text or "")),
        "链接": len(_URL.findall(text or "")),
        "时间": len(_YEAR.findall(text or "")),
    }
    return {key: count for key, count in categories.items() if count > 0}


def _extract_key_terms(text: str) -> Set[str]:
    """提取用于保留度评分的词：英文技术词 + 中文二元组 + 数字。

    中文二元组能捕获「学校/公司/项目名被替换」这类关键变动；
    数字（不跨行）用于评估量化成果是否被大范围删改。
    """
    source = _separate_glued_contacts(text or "")
    terms: Set[str] = set()
    for word in _EN_WORD.findall(source):
        normalized = _normalize(word)
        if "://" in normalized:
            normalized = normalized.split("://", 1)[1]
        if len(normalized) >= 3:
            terms.add(normalized)
    for cjk in _CJK.findall(source):
        for index in range(len(cjk) - 1):
            terms.add(cjk[index : index + 2])
    # 数字：单独用行内正则，避免 \s* 跨行粘连
    for num in re.findall(r"\d+(?:\.\d+)?%?", source):
        terms.add(_normalize(num))
    return terms


def _extract_added_facts(original: str, rewritten: str) -> List[str]:
    """提取重写稿中新出现、但原文没有出处的硬事实。

    优化只允许改写表达，不允许把 JD 或模型自己的知识写进用户履历。这里优先
    抓最容易被面试追问的类型：新联系方式/年份、新英文技术名、新数字、新角色
    以及明确的企业任职关系。中文自由表述无法做到完整 NER，但不会再把上述
    高风险新增内容当成「优化」静默保存。
    """
    original_norm = _normalize(original)
    rewritten_norm = _normalize(rewritten)
    added: List[str] = []

    critical_orig = _extract_critical_facts(original)
    critical_new = _extract_critical_facts(rewritten)
    added.extend(sorted(critical_new - critical_orig))

    original_en = {_normalize(word) for word in _EN_WORD.findall(original or "")}
    for word in _EN_WORD.findall(rewritten or ""):
        normalized = _normalize(word)
        if normalized in original_en:
            continue
        # 子串关系视为同一个词（Spring / SpringBoot），避免把正常扩写报成编造
        if any(name in normalized or normalized in name for name in original_en if len(name) >= 2):
            continue
        added.append(word)

    original_numbers = set(_NEW_FACT_NUMBER.findall(original or ""))
    added.extend(
        number for number in _NEW_FACT_NUMBER.findall(rewritten or "")
        if number not in original_numbers
    )

    added.extend(
        role for role in _ROLE_TERMS
        if role in rewritten_norm and role not in original_norm
    )
    for org in _ORG_CONTEXT.findall(rewritten or ""):
        if _normalize(org) and _normalize(org) not in original_norm:
            added.append(org)

    return list(dict.fromkeys(item for item in added if item))


def preservation_result(original: str, rewritten: str) -> Dict:
    """计算 rewritten 对 original 关键事实的保留情况。

    规则：
    - 硬事实（邮箱/电话/链接/四位年份）一个都不能丢，丢了直接不过；
    - **新增硬事实（原文没有的）直接不过** —— 编造比丢失更严重；
    - 软保留度（英文词/中文二元组/数字）>= 0.35 即通过：允许措辞升级、
      数字表述微调、删除少量无关内容，但整体替换学校/公司或大范围删改
      会显著拉低该值而被拒绝。

    返回 dict（与桌面其它判据层一致，不在 core 里引入 dataclass）：
    ``{score, passed, missing_facts, added_facts, fallback, critical_facts}``
    """
    critical_orig = _extract_critical_facts(original)
    critical_categories = _extract_critical_categories(original)
    rewritten_norm = _normalize(rewritten)

    missing_critical = [
        item for item in critical_orig if item not in rewritten_norm
    ]
    critical_ok = not missing_critical
    added_facts = _extract_added_facts(original, rewritten)

    terms_orig = _extract_key_terms(original)
    missing_terms = [t for t in terms_orig if t not in rewritten_norm]
    term_score = (
        (len(terms_orig) - len(missing_terms)) / len(terms_orig) if terms_orig else 1.0
    )

    passed = critical_ok and not added_facts and term_score >= _MIN_TERM_RETENTION

    return {
        "score": round(term_score, 4),
        "passed": passed,
        "missing_facts": missing_critical + missing_terms[:10],
        "added_facts": added_facts[:10],
        # fallback=True 表示「这份结果没有经过改写，是原文回退」，
        # 由回退分支显式置位；preservation_result 本身永远算的是「真算过」。
        "fallback": False,
        "critical_facts": critical_categories,
    }


def block_preserved(original: str, rewritten: str) -> bool:
    """单块是否通过保留校验（空块直接不过）。"""
    if not rewritten.strip():
        return False
    return preservation_result(original, rewritten)["passed"]
