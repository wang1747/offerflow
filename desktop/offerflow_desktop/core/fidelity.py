"""bullet 级事实保真：校验每条要点里的数字/时间/角色/技术栈是否来自用户输入
（自服务端 `modules/resume_generation/fidelity.py` 移植，逐字对齐）。

为什么需要它
-----------
`_build_result` 已经把**事实字段**用用户输入原样回填了：
学校/专业/学历（education）、公司/项目名、角色、起止时间（experiences[*]）。
所以 LLM 唯一还能"编造"的落点就是 `experiences[].bullets` 与 `summary`（纯表达层）。

其中**虚构量化成果**（"提升 40%"、"支撑日均 10 万次调用"、"服务 50 家客户"）
是最危险的一类失实：面试一问就穿，而且性质上属于诚信问题，比"写得不好"严重得多。

更糟的是，`core.quality._score_quant` 会因"缺少量化"扣分并把"补充数字"回灌给模型，
等于在**结构上鼓励编造数字**。所以本模块与 quality 的口径必须成对修改。

设计原则
--------
1. **纯函数、零 LLM**（正则 + 中文数字转换 + 词表），完全可单测、可复用。
2. 判据是「可溯源」而不是「看起来对不对」：某个数字（含单位）只要在**用户输入**里
   出现过，就算通过；凡是输入里找不到出处的一律记为疑似编造。
   宁可漏掉真话，不可放过假话 —— 简历上的数字，默认不可信。
3. 检出后不只报告，还给**确定性修复**：把疑似编造的数字就地降级。
   修复不依赖 LLM 是否听话 —— prompt 约束永远可能有漏网，代码兜底才是硬约束。
4. 不比较 JD：JD 里的数字（"3 年以上经验"）**不能**给 bullet 里的数字背书。

⚠️ 两份同源：服务端同一份在 `backend/app/modules/resume_generation/fidelity.py`，
**改动必须两边一起改**（判据见 `tests/test_fidelity.py`）。

⚠️ 与既有模块的分工（不要重复实现）：
- `core/preservation.py`：**块级**保真（原文块是否还在改写稿里），优化链路用。
- `services/resume_generation.check_generation_fidelity`：**字段级**枚举事实
  （技能/证书/联系方式有没有凭空新增）。
- 本模块：**bullet 级**（数字 / 年份 / 角色越权 / 编造技术栈）—— 上面两者都不覆盖。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ===== 单位表（长单位在前，避免 "个月" 被 "个" 抢先匹配）=====
_UNITS: Tuple[str, ...] = (
    "个百分点", "个月", "毫秒", "分钟", "小时",
    "万次", "亿次", "万人", "万元", "万人次",
    "GB", "MB", "TB", "KB", "ms", "kg", "km",
    "%", "％", "倍", "年", "月", "周", "天", "日", "秒",
    "人", "次", "条", "个", "项", "款", "家", "篇", "页", "字", "元", "美元",
    # 常用量词补全：缺了它们，「服务 50 位客户」「10 名成员」这类数字**完全不被识别**（漏检）
    "位", "名", "份", "组", "台", "户", "笔", "张", "套", "门", "期", "轮",
    "类", "种", "层", "只", "件",
)

# 「N万用户」这类没有量词的数量短语：只有当数字以 万/千/亿 结尾时，后面才允许直接跟名词。
# 不能把名词并进 _UNITS —— 「统一接口」「统一系统」会被误读成「统1接口」，制造假阳性。
_CN_QUANT_NOUNS: Tuple[str, ...] = (
    "用户", "客户", "请求", "接口", "模块", "系统", "并发", "实例",
    "记录", "订单", "页面", "文件", "任务", "功能", "环节", "角色",
    "团队", "机型", "节点", "库表",
)

_UNIT_CANON = {"％": "%"}

# 「纯量词」：只承载"这里有个数量"，不承载事实本身。
# 归一化时**必须剥掉**，否则同一事实的两种写法会归一到两个不同 token：
#   「十万用户」（中文无量词）→ 100000     vs  「100000 个用户」→ 100000个   ← 不等！
# 后果是**合法数字被判成编造并删掉**（生产实测确认过的失效模式）。
# 反例（不剥）：`%` / `倍` / `ms` / `年` / `月` / `分钟` —— 这些单位的语义不可替换。
_COUNT_UNITS: frozenset = frozenset({
    "人", "次", "条", "个", "项", "款", "家", "篇", "页", "字", "元", "美元",
    "位", "名", "份", "组", "台", "户", "笔", "张", "套", "门", "期", "轮",
    "类", "种", "层", "只", "件",
})

_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))

# 数字 + 单位（如 40% / 3个月 / 800ms / 10万次 展开后为 100000次）
# 注意：**不能**在单位后加 (?![\d])，否则 "2023年09月" 里的 "2023年" 会被判为非法匹配而丢掉。
_NUM_UNIT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(" + _UNIT_ALT + r")",
    re.IGNORECASE,
)

# 无单位的裸数字：只认 4 位以上，且排除 1900~2099 的年份（年份单独校验，避免重复报）
_BARE_NUM_RE = re.compile(r"(?<![\d.])(\d{4,})(?![\d])")

# 年份（4 位，19xx/20xx）
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# 中文数字
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS = {"万": 10000, "亿": 100000000}
_CN_CHARS = "".join(_CN_DIGITS) + "".join(_CN_SMALL_UNITS) + "".join(_CN_BIG_UNITS)

# 只在「中文数字 + 量词」时展开，避免把「十分满意」「第三方」这类词误伤
_CN_RUN_BEFORE_UNIT_RE = re.compile(
    r"([" + _CN_CHARS + r"]+)(?=\s*(?:" + _UNIT_ALT + r"))"
)
# 「十万用户」：中文数字以 万/千/亿 结尾且后面直接跟名词
_CN_QUANT_NOUN_ALT = "|".join(
    re.escape(n) for n in sorted(_CN_QUANT_NOUNS, key=len, reverse=True)
)
_CN_RUN_BEFORE_NOUN_RE = re.compile(
    r"([" + _CN_CHARS + r"]*[万千亿])(?=\s*(?:" + _CN_QUANT_NOUN_ALT + r"))"
)
_CN_PERCENT_RE = re.compile(r"百分之\s*([" + _CN_CHARS + r"]+)")
_CN_BIG_MULTIPLIER = {"万": 10000, "千": 1000, "亿": 100000000}
_CN_MULT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([万千亿])")

# 日期归一：2023-09 / 2023.09 / 2023/09 → 2023年09月（让年份+月份也能作为可溯源数字）
_DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[-/.]\s*(\d{1,2})")

# 结果从句的起始词：编造的数字几乎都挂在这类"价值从句"上
RESULT_CLAUSE_PREFIXES: Tuple[str, ...] = (
    "支撑", "覆盖", "提升", "提高", "降低", "缩短", "节省", "减少", "达到",
    "服务", "保障", "推动", "带动", "增长", "拉高", "拉低", "优化到", "压缩到", "积累",
)

# 删完数字后，剩下这些尾巴的词 = 句子被削残了，不能留
_DANGLING_TAILS: Tuple[str, ...] = (
    "从", "到", "至", "达", "约", "近", "超", "的", "了", "和", "与", "及",
    "日均", "平均", "单日", "每日", "每月", "每周", "共计", "累计", "高达", "多达", "已达",
    "支撑", "覆盖", "提升", "提高", "降低", "缩短", "节省", "减少", "服务", "保障",
    "，", ",", "、", "；", ";", "。",
)
# 「修饰词 + 名词」结尾：说明修饰词的对象（数字）被删了，只剩光秃秃的定语
_DANGLING_MODIFIER_TAIL_RE = re.compile(
    r"(?:日均|平均|单日|每日|每月|每周|共计|累计|高达|多达|已达)[^0-9]{2,4}$"
)

# 数字前的程度/频次修饰词，随数字一起删
_QUANT_PREFIX_RE = re.compile(
    r"(约|近|超过|超|高达|多达|累计|共计|日均|平均|单日|每月|每天|已有|已达|仅|足足)$"
)
# 数字后的余量词
_QUANT_SUFFIX_RE = re.compile(r"^\s*(余|多|左右|以上|以下|之内|以内|不等)")

# 角色越权词：bullet 里自称这些角色，但用户填的 role 里没有对应表述 → 警告
ROLE_CLAIM_WORDS: Tuple[str, ...] = (
    "技术负责人", "项目负责人", "负责人", "主导者", "牵头人", "牵头",
    "架构师", "组长", "队长", "owner", "Owner", "OWNER", "主负责人", "总负责",
    "独立负责", "全权负责", "首席",
)
# 用户 role 里出现这些词，视为"有权这么自称"
ROLE_ALLOW_WORDS: Tuple[str, ...] = (
    "负责", "主管", "组长", "队长", "牵头", "架构", "owner", "leader", "lead",
    "经理", "总监", "首席",
)

# 拉丁技术词：bullet 里出现、用户输入里找不到的技术名（编造技术栈的常见形态）
_LATIN_RE = re.compile(r"(?<![A-Za-z0-9+#.])([A-Za-z][A-Za-z0-9+#.\-]{1,})(?![A-Za-z0-9])")
_LATIN_STOP: Set[str] = {
    "and", "the", "with", "for", "to", "in", "on", "of", "or", "by", "at", "as", "is",
    "a", "an", "vs", "etc", "no", "not", "all", "from", "into", "using", "use", "based",
    "m", "s", "kg", "km", "gb", "mb", "tb", "kb", "ms",
}

# ===== 数据模型 =====


@dataclass
class BulletFidelityReport:
    """bullet 级保真报告。hard 项 = 事实性失实（必须处理）；其余为风险提示。"""

    issues: List[Dict[str, Any]] = field(default_factory=list)
    fabricated_numbers: List[str] = field(default_factory=list)
    feedback_hint: str = ""

    @property
    def hard_count(self) -> int:
        return sum(1 for i in self.issues if i.get("hard"))

    @property
    def passed(self) -> bool:
        return self.hard_count == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "hard_count": self.hard_count,
            "issues": self.issues,
            "fabricated_numbers": list(dict.fromkeys(self.fabricated_numbers)),
        }


# ===== 文本归一化 =====


def _cn_to_int(chars: str) -> Optional[int]:
    """把中文数字串转成整数（支持 十/百/千/万/亿 组合）。

    「十三」→13、「三十」→30、「一百二十万」→1200000、「三」→3。
    无法解析（含非数字字符）时返回 None。
    """
    if not chars:
        return None
    total = 0
    section = 0
    number = 0
    for ch in chars:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_SMALL_UNITS:
            unit = _CN_SMALL_UNITS[ch]
            section += (number or 1) * unit
            number = 0
        elif ch in _CN_BIG_UNITS:
            unit = _CN_BIG_UNITS[ch]
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def _expand_cn_percent(text: str) -> str:
    def _sub(m: "re.Match") -> str:
        value = _cn_to_int(m.group(1))
        return f"{value}%" if value is not None else m.group(0)

    return _CN_PERCENT_RE.sub(_sub, text)


def _expand_cn_numerals(text: str) -> str:
    def _sub(m: "re.Match") -> str:
        value = _cn_to_int(m.group(1))
        # 纯量词（「万次」「亿级」）解析出来是 0，不是数字，不能展开
        if not value:
            return m.group(0)
        return str(value)

    # 先处理「十万用户」这类无量词短语，再处理「三个模块」这类带量词的
    text = _CN_RUN_BEFORE_NOUN_RE.sub(_sub, text)
    return _CN_RUN_BEFORE_UNIT_RE.sub(_sub, text)


def _expand_big_multiplier(text: str) -> str:
    """把「10万」这类写法展开成「100000」，让「10万次」与「100000 次」可比。"""

    def _sub(m: "re.Match") -> str:
        factor = _CN_BIG_MULTIPLIER[m.group(2)]
        value = float(m.group(1)) * factor
        return str(int(value)) if value == int(value) else str(value)

    return _CN_MULT_RE.sub(_sub, text)


def normalize_fact_text(text: str) -> str:
    """把一段文本归一化到「可比对」的形态（数字/日期写法统一）。"""
    t = text or ""
    t = _DATE_RE.sub(r"\1年\2月", t)
    t = _expand_cn_percent(t)
    t = _expand_cn_numerals(t)
    t = _expand_big_multiplier(t)
    return t


def _number_spans(text: str) -> List[Tuple[int, int, str, str]]:
    """抽出所有「数字 token」：返回 (start, end, 原文, 归一化token)。"""
    spans: List[Tuple[int, int, str, str]] = []
    for m in _NUM_UNIT_RE.finditer(text):
        raw_num, unit = m.group(1), m.group(2)
        unit = _UNIT_CANON.get(unit, unit)
        # 纯量词剥掉只留数值（见 _COUNT_UNITS 的说明）；有语义的单位保留
        token = raw_num if unit in _COUNT_UNITS else f"{raw_num}{unit}"
        spans.append((m.start(), m.end(), m.group(0), token))
    for m in _BARE_NUM_RE.finditer(text):
        value = m.group(1)
        if 1900 <= int(value) <= 2099:
            continue  # 年份单独校验
        spans.append((m.start(), m.end(), m.group(0), value))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    # 去掉被大 span 包含的小 span（如 "10万次" 已含 "100000"）
    result: List[Tuple[int, int, str, str]] = []
    last_end = -1
    for span in spans:
        if span[0] < last_end:
            continue
        result.append(span)
        last_end = span[1]
    return result


def canonical_numbers(text: str) -> Set[str]:
    """一段文本里所有「可溯源数字」的归一化集合。

    归一化后同一件事只有一种写法：
    「三个」/「3个」/「3 位」→ 3 ；
    「十万用户」/「10万用户」/「100000 个用户」→ 100000 ；
    「10万次」/「100000 次」→ 100000 ；
    「百分之四十」/「40%」→ 40%（`%` 有语义，保留）。
    """
    normalized = normalize_fact_text(text)
    return {span[3] for span in _number_spans(normalized)}


def _canonical_years(text: str) -> Set[str]:
    return {m.group(1) for m in _YEAR_RE.finditer(normalize_fact_text(text))}


# ===== 确定性修复 =====


def _enclosing_clause(text: str, start: int, end: int) -> Optional[Tuple[int, int, str]]:
    """找出包含 [start, end) 的最小分隔符从句（按中英文逗号/分号/句号切）。"""
    for m in re.finditer(r"[^，,；;。！？!?\n]+", text):
        if m.start() <= start and m.end() >= end:
            return m.start(), m.end(), m.group(0)
    return None


def _is_usable(text: str) -> bool:
    """删除数字后的残留文本是否还读得通（读不通的宁可整条丢弃）。"""
    s = (text or "").strip()
    if len(s) < 6:
        return False
    # 句末标点不算"残缺"，判断前先剥掉（否则正常的句号结尾会被误判）
    body = s.rstrip("。！？!?")
    if not body:
        return False
    if body.endswith(_DANGLING_TAILS):
        return False
    # 「支撑日均调用」这种：修饰词的对象（数字）被删了，只剩光秃秃的定语 → 读不通
    if _DANGLING_MODIFIER_TAIL_RE.search(body):
        return False
    return True


def _remove_span(text: str, start: int, end: int) -> str:
    """删掉 [start, end) 这段数字。

    两个候选方案，取还读得通、且保留内容更多的那一版：
      A. 只删「数字 + 量词」（尽量保留句子其余部分）
      B. 删掉整个逗号从句（当该从句的"信息内核"就是这个数字时更干净）
    两个方案都读不通 → 返回空串，由调用方决定丢弃整条要点。
    """
    m = _QUANT_PREFIX_RE.search(text[:start])
    if m:
        start = m.start()
    m2 = _QUANT_SUFFIX_RE.match(text[end:])
    if m2:
        end += m2.end()

    span_only = _tidy(text[:start] + text[end:])

    clause_removed = ""
    clause = _enclosing_clause(text, start, end)
    if clause:
        cstart, cend, _ctext = clause
        cut = cstart
        while cut > 0 and text[cut - 1] in " \t":
            cut -= 1
        if cut > 0 and text[cut - 1] in "，,；;、。":
            cut -= 1
        clause_removed = _tidy(text[:cut] + text[cend:])

    candidates = [c for c in (span_only, clause_removed) if _is_usable(c)]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _tidy(text: str) -> str:
    """清掉删除后留下的空标点/多余空格。"""
    t = text or ""
    t = re.sub(r"[ \t]{2,}", " ", t)
    # 中文字之间的空格是 LLM 为了标数字加的，数字删掉后要收回
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，,；;。、！？])", "", t)
    t = re.sub(r"\s+([，,；;。、！？!?）)])", r"\1", t)
    t = re.sub(r"([（(])\s+", r"\1", t)
    t = re.sub(r"([，,；;、])\s*(?=[，,；;、。])", "", t)
    t = re.sub(r"([，,])\1+", r"\1", t)
    t = re.sub(r"^[\s，,；;、。]+", "", t)
    t = re.sub(r"[\s，,；;、]+$", "", t)
    t = t.replace("，。", "。").replace("、。", "。")
    return t.strip()


def sanitize_text(text: str, allowed: Set[str]) -> Tuple[str, List[str]]:
    """把文本里所有"不可溯源"的数字就地降级，返回 (新文本, 被删掉的数字原文列表)。

    幂等：再次调用同一份 allowed 不会再改动文本。
    """
    if not text or not text.strip():
        return text or "", []

    current = normalize_fact_text(text)
    if not [s for s in _number_spans(current) if s[3] not in allowed]:
        return text, []  # 没有可疑数字 → 原文返回，绝不做任何改写

    seen_raw: List[str] = []
    for _ in range(10):
        fabricated = [s for s in _number_spans(current) if s[3] not in allowed]
        if not fabricated:
            break
        start, end, raw, _token = fabricated[0]
        new_text = _remove_span(current, start, end)
        seen_raw.append(raw.strip())
        if new_text == current:  # 兜底：删不掉就退出，绝不死循环
            break
        current = new_text

    final = _tidy(current)
    # 只报告"确实消失了"的数字（从句整删时可能连带带走了别的可疑数字）
    removed = [r for r in dict.fromkeys(seen_raw) if r and r not in final]
    return final, removed


# ===== 逐 bullet 校验 =====


def _iter_bullets(structured: Dict[str, Any]):
    for i, exp in enumerate(structured.get("experiences") or []):
        if not isinstance(exp, dict):
            continue
        for j, b in enumerate(exp.get("bullets") or []):
            if isinstance(b, str) and b.strip():
                yield i, j, f"experiences[{i}].bullets[{j}]", b


def _allowed_roles(source_roles: Optional[Sequence[str]]) -> bool:
    """用户填的 role 里是否有"可以自称负责人"的表述。"""
    joined = " ".join(r or "" for r in (source_roles or []))
    lowered = joined.lower()
    return any(w.lower() in lowered for w in ROLE_ALLOW_WORDS)


def check_bullet_fidelity(
    structured: Dict[str, Any],
    source_text: str,
    source_roles: Optional[Sequence[str]] = None,
) -> BulletFidelityReport:
    """校验 structured 的 bullets / summary 里有没有"用户没提供过"的事实。

    - 数字（含百分比/倍数/人天/规模/数量）：输入里找不到出处 → **hard**（疑似编造量化成果）
    - 年份：输入（含教育/经历的起止时间）里没有 → **hard**（疑似编造时间）
    - 角色越权：自称负责人/牵头人/架构师，但用户填的 role 不支持 → warn
    - 技术栈：bullet 出现拉丁技术名，用户输入里完全没有 → warn
    """
    report = BulletFidelityReport()
    allowed_numbers = canonical_numbers(source_text)
    allowed_years = _canonical_years(source_text)
    normalized_source = normalize_fact_text(source_text).lower()
    role_ok = _allowed_roles(source_roles)
    fabricated_raw: List[str] = []

    # --- summary：只查数字（summary 的写法自由度最大，也最容易"总结出"新数字）---
    summary = structured.get("summary")
    if isinstance(summary, str) and summary.strip():
        normalized = normalize_fact_text(summary)
        for _s, _e, raw, token in _number_spans(normalized):
            if token in allowed_numbers:
                continue
            fabricated_raw.append(raw.strip())
            report.issues.append({
                "section": "summary",
                "kind": "number",
                "hard": True,
                "detail": f"个人总结出现「{raw.strip()}」，用户原始信息里没有这个数字",
                "suggestion": "删除该数字，或改用不含具体数字的价值表达（如「支撑日常使用」）",
            })

    # --- bullets ---
    for exp_index, _bullet_index, loc, bullet in _iter_bullets(structured):
        normalized = normalize_fact_text(bullet)

        for _s, _e, raw, token in _number_spans(normalized):
            if token in allowed_numbers:
                continue
            fabricated_raw.append(raw.strip())
            report.issues.append({
                "section": loc,
                "kind": "number",
                "hard": True,
                "detail": f"出现「{raw.strip()}」，用户原始信息里没有这个数字",
                "suggestion": "删除该数字，或改用不含具体数字的价值表达（如「支撑日常使用」）",
            })

        reported_numbers = {i["detail"] for i in report.issues if i["section"] == loc}
        for m in _YEAR_RE.finditer(normalized):
            year = m.group(1)
            if year in allowed_years:
                continue
            if any(year in d for d in reported_numbers):
                continue  # 已被数字项覆盖，不重复报
            report.issues.append({
                "section": loc,
                "kind": "year",
                "hard": True,
                "detail": f"出现「{year} 年」，与用户提供的经历/教育时间对不上",
                "suggestion": "年份必须与用户填写的起止时间一致，不要在要点里自创时间",
            })

        if not role_ok:
            # 同一条 bullet 只报最长的一个身份词（「技术负责人」不该再报一次「负责人」）
            hit = max((w for w in ROLE_CLAIM_WORDS if w in bullet), key=len, default="")
            if hit:
                report.issues.append({
                    "section": loc,
                    "kind": "role",
                    "hard": False,
                    "detail": f"自称「{hit}」，但用户填写的角色不支持这个身份",
                    "suggestion": "去掉身份声明，改为描述自己实际做的工作与结果",
                })

        for m in _LATIN_RE.finditer(bullet):
            token_l = m.group(1).rstrip(".-")
            if token_l.lower() in _LATIN_STOP:
                continue
            if len(token_l) < 2:
                continue
            if token_l.lower() in normalized_source:
                continue
            report.issues.append({
                "section": loc,
                "kind": "tech",
                "hard": False,
                "detail": f"出现技术名「{token_l}」，用户原始信息里没有提到过",
                "suggestion": f"确认是否真的用过「{token_l}」；没写过就从要点和技能里一并去掉",
            })

        # 同一 section 同类问题只报一次，避免刷屏
        _dedupe_issues(report)

    report.fabricated_numbers = list(dict.fromkeys(fabricated_raw))
    report.feedback_hint = _build_feedback(report)
    return report


def _dedupe_issues(report: BulletFidelityReport) -> None:
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in report.issues:
        key = (item.get("section"), item.get("kind"), item.get("detail"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    report.issues = unique


def _build_feedback(report: BulletFidelityReport) -> str:
    """把保真违规整理成可直接追加到 prompt 的反馈（供重写时使用）。"""
    if not report.issues:
        return ""
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for item in report.issues:
        by_kind.setdefault(item["kind"], []).append(item)

    label = {
        "number": "编造数字（最严重）",
        "year": "编造时间",
        "role": "角色越权",
        "tech": "编造技术栈",
    }
    parts = [
        "你上一版简历里有「用户原始信息中根本不存在的事实」，这属于编造，必须修正："
    ]
    for kind, items in by_kind.items():
        lines = [f"{it['section']}：{it['detail']}" for it in items[:6]]
        parts.append(f"【{label.get(kind, kind)}】\n" + "\n".join("- " + l for l in lines))
    parts.append(
        "硬性要求：\n"
        "1. 所有数字（百分比/倍数/次数/规模/人天/客户数/耗时）必须能在用户原始信息里逐字找到出处；"
        "找不到出处的一律不许写，改用不含具体数字的价值表达（如「支撑日常使用」「覆盖核心链路」）。\n"
        "2. 年份与时间必须与用户填写的起止时间一致。\n"
        "3. 不要给自己安上用户没写过的角色身份（负责人/牵头人/架构师）。\n"
        "4. 不要引入用户没提到过的技术名。\n"
        "5. 宁可少写一点，也绝不允许编造。"
    )
    return "\n\n".join(parts)


def sanitize_structured(
    structured: Dict[str, Any],
    source_text: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """就地清掉 structured 里不可溯源的数字（含 summary 与全部 bullets）。

    返回 (同一个 structured 对象, 被删掉的数字原文列表)。
    """
    allowed = canonical_numbers(source_text)
    removed: List[str] = []

    summary = structured.get("summary")
    if isinstance(summary, str) and summary.strip():
        new_summary, r = sanitize_text(summary, allowed)
        if r:
            structured["summary"] = new_summary
            removed.extend(r)

    for exp in structured.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        bullets = exp.get("bullets")
        if not isinstance(bullets, list):
            continue
        kept: List[str] = []
        for bullet in bullets:
            if not isinstance(bullet, str) or not bullet.strip():
                continue
            new_bullet, r = sanitize_text(bullet, allowed)
            removed.extend(r)
            # 删完只剩残句（如「支撑」）→ 整条丢弃，不留断句
            if len(new_bullet) >= 6:
                kept.append(new_bullet)
        exp["bullets"] = kept

    return structured, list(dict.fromkeys(removed))
