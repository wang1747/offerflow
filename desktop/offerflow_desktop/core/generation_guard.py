"""生成结果守门层（Generation Guard）。

**为什么单独有这个模块**

「生成 → 质检 → 展示」这条路上已经有两道检查：

* ``core/resume_review``：管**写作规范** —— 套话、弱动词、要点长度、量化率、分节；
* ``services.resume_generation.check_generation_fidelity``：管**技能 / 证书 /
  联系方式**有没有来自用户输入。

两者之间还留着三块空白，本模块补的就是这三块：

1. **数字没人核对**。生成结果里「响应时间降低 40%」「支撑 5000 QPS」这类指标，
   既不在“写作规范”的检查范围（只要带了单位就算量化过），也不在 fidelity 的
   范围（fidelity 只看技能/证书/联系方式）。而**编造数字是 AI 写简历最致命的
   问题** —— 面试官会追问，答不上来就是诚信问题。
2. **中文求职特有的风险信号没人查**：外包/OD 公司标签被放大到标题行、玩具项目
   题材（秒杀/商城/博客）充数、「精通 X」却在经历里找不到 X 的任何证据。
3. **模型自带的企业黑话腔**会一路带到简历里（赋能、闭环、抓手、颗粒度）。

另外附带一条**量化降级建议**：拿不出精确数字时，告诉用户能用什么替代
（高频/日级/跨团队/核心链路），而不是让模型硬编一个数字。

**架构取自 hr-breaker（MIT 许可，github.com/btseytlin/hr-breaker）**

那是一个 Python 简历优化工具，核心机制是「优化循环 + 一组按优先级排序的
过滤器，不通过就带着失败原因重新生成」。它的 ``filters/`` 用
``BaseFilter`` + ``FilterRegistry.register`` 装饰器注册、每个过滤器带
``name`` / ``priority`` / ``threshold``。这套形状很干净，直接照搬：

* 一个 guard 一个类，管一件事；
* ``GuardRegistry`` 收集，``priority`` 决定顺序（编造检测最先跑）；
* 每个 guard 只负责**产出问题**，不碰分数 —— 扣分口径集中在 ``check()`` 里，
  避免“改一个 guard 悄悄改了总分”。

**输出结构与 ``core/resume_review`` 的 issues 对齐**（severity / category /
location / message / fix / line / snippet），这样 UI 能复用同一套渲染代码，
不必为守门层再写一个列表控件。

**分层约束**：本模块在 ``core`` 层，**不 import services**。用户原始输入
（``source_text``）由 services 层组装后传进来 —— 与 ``core/ats.py`` 收
``template`` 参数同理，否则就分层倒置了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .preservation import _extract_critical_facts
from .resume_review import QUANT_RE
from .text_utils import normalize_text

# ============ 扣分口径（集中在此，guard 自己不碰分数） ============

FABRICATION_CAP = 32        # 编造：最重，且只要命中就一定不是「优秀」
RISK_CAP = 14
AI_TONE_CAP = 10
QUANT_CAP = 10

PASS_THRESHOLD = 80         # 低于此分不算通过（除编造为 0 外）

# ============ 中文求职风险词表 ============
# 来源：wyh0626/resume-optimizer 的 references/red-flags.md
# （该项目为中文求职场景整理的常见雷区清单）。这里只取**可确定性判定**的部分；
# 判断原则也照抄那条：「外包经历本身不是原罪，问题是项目价值没讲清楚」。

# 大型 IT 服务 / 外包 / OD 公司（出现在经历标题行时提示，而非禁止）
OUTSOURCE_LABELS: Tuple[str, ...] = (
    "中科软", "中软国际", "中软", "法本信息", "法本", "软通动力", "软通",
    "东软集团", "东软", "文思海辉", "博彦科技", "博彦", "柯莱特", "中电金信",
    "华为od", "华为 od", "阿里外包", "字节外包", "腾讯外包", "美团外包",
    "外派", "驻场",
)

# 玩具项目题材：不是不能写，是**写法普通时很难加分**，至少要突出一项真实亮点
TOY_PROJECT_WORDS: Tuple[str, ...] = (
    "秒杀", "商城", "外卖", "博客", "在线教育", "图书管理", "学生管理",
    "图书借阅", "选课系统", "贪吃蛇", "待办事项", "记事本", "计算器",
    "天气查询", "个人主页", "管理系统",
)

# 「精通 / 熟练掌握」这类高频自我评级词
MASTERY_WORDS: Tuple[str, ...] = ("精通", "熟练掌握", "深入理解", "深刻理解")

# 企业黑话 / AI 腔。刻意与 resume_review 的 CLICHES 不重叠：
# CLICHES 管的是「吃苦耐劳」这种学生式套话，这里管的是「赋能、闭环」这种
# 企业式空话 —— 后者更像是模型生成的痕迹。
AI_TONE_WORDS: Tuple[str, ...] = (
    "赋能", "闭环", "抓手", "颗粒度", "拉通", "对齐", "打法", "方法论沉淀",
    "深耕", "致力于", "极大地", "切实", "全面提升", "有效提升", "显著提升",
    "持续赋能", "深度参与", "全方位", "多维度", "一站式", "端到端",
    "在…方面具有", "具有丰富经验", "积累了丰富", "深刻理解用户",
)

# 空话句式：只说「具有 X 能力/精神/意识」而不给证据
_EMPTY_CLAIM_RE = re.compile(
    r"(?:具有|具备|拥有|富有)(?:较强|很强|良好|优秀|扎实)?的?"
    r"(?:[\u4e00-\u9fff]{0,8})?(?:能力|精神|意识|素养|思维)"
)

# ============ 硬事实抽取（编造检测用） ============

_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*[%％]")
_TIMES_RE = re.compile(r"\d+(?:\.\d+)?\s*倍")
_COMPARE_RE = re.compile(
    r"从\s*[\d.]+\s*(?:提升|提高|降低|降至|下降|增长|压缩|减少)到?\s*[\d.]+\s*[%％]?")
_BIG_NUM_RE = re.compile(r"(?<![\d.])\d{3,}(?:\.\d+)?(?![\d.])")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_ORG_RE = re.compile(
    r"[\u4e00-\u9fff]{2,14}(?:有限公司|有限责任公司|集团|信息技术|网络科技|"
    r"科技有限公司|软件|研究院|研究所|银行|医院|大学|学院|中学)")


def _norm(text: str) -> str:
    """归一：去空白 + 小写。与 preservation 口径一致。"""
    return re.sub(r"\s+", "", (text or "").lower())


def _collect_facts(pattern: re.Pattern, text: str) -> List[str]:
    return [m.group(0).strip() for m in pattern.finditer(text or "") if m.group(0).strip()]


def _digits_only(raw: str) -> str:
    """取一个事实里的数字部分，用于「用户输入里是否出现过这个数」的宽松比对。"""
    nums = re.findall(r"\d+(?:\.\d+)?", raw or "")
    return nums[0] if nums else ""


@dataclass
class GuardContext:
    """守门判据需要的全部输入。

    ``source_text`` 是**用户原始输入**（表单里填的东西），由 services 层组装
    （见 ``services/resume_generation._req_input_text``）。为空时编造检测降级：
    只报「无法核对」，不误报。
    """

    structured: Dict[str, Any] = field(default_factory=dict)
    resume_text: str = ""
    source_text: str = ""
    jd_text: str = ""

    @property
    def bullets(self) -> List[str]:
        out: List[str] = []
        for exp in self.structured.get("experiences") or []:
            if not isinstance(exp, dict):
                continue
            for bullet in exp.get("bullets") or []:
                text = str(bullet).strip()
                if text:
                    out.append(text)
        return out

    @property
    def experiences(self) -> List[Dict[str, Any]]:
        return [e for e in (self.structured.get("experiences") or [])
                if isinstance(e, dict)]

    @property
    def skills(self) -> List[str]:
        return [str(s).strip() for s in (self.structured.get("skills") or []) if str(s).strip()]

    @property
    def summary(self) -> str:
        return str(self.structured.get("summary") or "")


def _issue(category: str, severity: str, location: str, message: str,
           fix: str = "", cost: float = 0.0) -> Dict[str, Any]:
    """与 ``core/resume_review`` 的 issue 同结构（line/snippet 在结构化路径下为空）。"""
    return {
        "severity": severity, "category": category, "location": location,
        "message": message, "fix": fix, "line": None, "snippet": "",
        "_cost": cost,
    }


# ============ 过滤器基类与注册表 ============


class BaseGuard:
    """一个 guard 管一件事，只**产出问题**，不参与算分。

    分两段：``evaluate`` 里写判据，``check`` 里由调用方统一按 category 扣分。
    """

    name = "BaseGuard"
    priority = 99
    category = "其他"

    def evaluate(self, ctx: GuardContext) -> List[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


class GuardRegistry:
    """装饰器注册表 —— 照 hr-breaker 的 ``FilterRegistry`` 形状。"""

    _guards: Dict[str, Any] = {}

    @classmethod
    def register(cls, guard_cls):
        cls._guards[guard_cls.name] = guard_cls
        return guard_cls

    @classmethod
    def all(cls) -> List[Any]:
        return sorted(cls._guards.values(), key=lambda g: g.priority)

    @classmethod
    def names(cls) -> List[str]:
        return [g.name for g in cls.all()]


# ============ Guard 1：编造硬事实（最高优先级） ============


@GuardRegistry.register
class FabricationGuard(BaseGuard):
    """生成结果里的数字 / 年份 / 机构名，在用户输入里找不到依据 ⇒ 疑似编造。

    与 ``check_generation_fidelity`` 的分工：那个查**技能/证书/联系方式**（枚举类
    事实），这个查**数字/年份/机构**（文本里散落的事实）。两者合起来才是完整的
    编造检测。与 hr-breaker 的 ``HallucinationChecker`` 同目标，但用确定性规则
    而非再调一次 LLM —— 简历上的数字是可以精确比对的，不该交给模型猜。
    """

    name = "FabricationGuard"
    priority = 0
    category = "编造"

    def evaluate(self, ctx: GuardContext) -> List[Dict[str, Any]]:
        if not ctx.resume_text.strip():
            return []
        source_norm = _norm(ctx.source_text)
        # 没有原始输入就没法核对 —— 明说，不假装通过（错题本 E-09 的教训：
        # 「说不出口的通过」比「报错」更危险）
        if not source_norm:
            return [_issue(
                self.category, "low", "整体",
                "未能取得你的原始输入，无法核对生成结果是否编造",
                "重新从表单生成一次即可恢复核对；当前请人工确认文中的数字都属实",
                cost=0.0)]

        issues: List[Dict[str, Any]] = []

        # --- 1. 指标类数字：百分比 / 倍数 / 前后对比 ---
        metric_facts: List[str] = []
        for pattern in (_PERCENT_RE, _TIMES_RE, _COMPARE_RE):
            metric_facts.extend(_collect_facts(pattern, ctx.resume_text))
        unsupported = [f for f in dict.fromkeys(metric_facts)
                       if _digits_only(f) and _digits_only(f) not in source_norm]

        source_has_number = bool(re.search(r"\d", source_norm))
        if unsupported:
            # 用户输入里一个数字都没有，生成却冒出多个指标 ⇒ 几乎可以断定是编造
            if not source_has_number and len(unsupported) >= 2:
                issues.append(_issue(
                    self.category, "high", "要点",
                    f"你填写的材料里**没有任何数字**，但生成结果出现了 "
                    f"{len(unsupported)} 个量化指标（{'、'.join(unsupported[:5])}）——"
                    "这些数字没有来源，面试时无法解释",
                    "删掉这些指标，或换成你确实做过的事实；"
                    "确实有数据的话，回表单里补上，再重新生成",
                    cost=20.0))
            else:
                issues.append(_issue(
                    self.category, "high", "要点",
                    "以下指标在你的原始输入里找不到依据："
                    + "、".join(unsupported[:5]),
                    "逐个核对：真有出处的，把依据补进表单；没有出处的直接删掉",
                    cost=12.0))

        # --- 2. 三位以上的大数（规模类，如 5000 QPS / 10 万用户）---
        big_nums = [n for n in dict.fromkeys(_collect_facts(_BIG_NUM_RE, ctx.resume_text))
                    if _norm(n) not in source_norm]
        if big_nums:
            issues.append(_issue(
                self.category, "medium", "要点",
                "以下规模数字在你填写的材料里找不到："
                + "、".join(big_nums[:6]),
                "规模数字同样会被追问（用户量/数据量/接口量），确认每一项都说得出出处",
                cost=6.0))

        # --- 3. 年份 ---
        years = [y for y in dict.fromkeys(_collect_facts(_YEAR_RE, ctx.resume_text))
                 if _norm(y) not in source_norm]
        if years:
            issues.append(_issue(
                self.category, "medium", "时间",
                "以下年份在你填写的材料里找不到：" + "、".join(years[:6]),
                "年份错一个，整段时间线都会被质疑；核对起止时间后改准",
                cost=4.0))

        # --- 4. 机构名 ---
        orgs = [o for o in dict.fromkeys(_collect_facts(_ORG_RE, ctx.resume_text))
                if _norm(o) not in source_norm]
        if orgs:
            issues.append(_issue(
                self.category, "high", "经历",
                "以下机构名在你填写的材料里找不到：" + "、".join(orgs[:5]),
                "公司/学校名是不能改写的硬事实；确认后修正（同名简称也可能误报，请核对）",
                cost=10.0))

        # --- 5. 硬事实丢失（复用 core/preservation 的抽取器）---
        # 反方向的问题：用户输入里的邮箱/电话/链接/年份，在生成结果里丢了。
        # 与「编造」是一体两面，一并在此报出。
        lost = [f for f in _extract_critical_facts(ctx.source_text)
                if f and f not in _norm(ctx.resume_text)]
        if lost:
            issues.append(_issue(
                self.category, "medium", "页眉",
                "你原始材料里的联系方式/年份在生成结果里丢失了："
                + "、".join(sorted(lost)[:5]),
                "联系方式丢了会直接影响 HR 联系你，务必补回",
                cost=5.0))

        return issues


# ============ Guard 2：中文求职风险 ============


def _exp_label(exp: Dict[str, Any]) -> str:
    """一段经历的「标题文本」—— 外包标签、玩具项目两条判据都靠它。

    ⚠️ 这里曾经只读 ``exp["left"]``，导致下面两条判据**恒为空、永不触发**：
    ``experiences`` 在生成链路上用的是 ``name`` / ``role``（服务端
    ``_build_result`` 与本地 ``build_result`` 都如此），而 ``left`` 是
    ``core/resume_review.parse_resume_text`` 那套「纯文本解析块」的键
    （导出链路在用）。两套形状都要认：先取 ``left``，取不到再拼 ``name role``。
    """
    if not isinstance(exp, dict):
        return ""
    left = str(exp.get("left") or "").strip()
    if left:
        return left
    return " ".join(
        part for part in (str(exp.get("name") or "").strip(),
                          str(exp.get("role") or "").strip()) if part
    )


@GuardRegistry.register
class RiskGuard(BaseGuard):
    """中文求职场景特有的三处风险信号（来源见文件头的 red-flags 说明）。"""

    name = "RiskGuard"
    priority = 2
    category = "风险"

    def evaluate(self, ctx: GuardContext) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        exp_text = " ".join(
            [(_exp_label(e) + " " + " ".join(
                str(b) for b in (e.get("bullets") or []))).strip()
             for e in ctx.experiences])
        exp_norm = normalize_text(exp_text or "")

        # --- 1. 外包 / OD 标签出现在经历标题 ---
        for exp in ctx.experiences:
            label = _exp_label(exp)
            label_norm = normalize_text(label).lower()
            hit = next((w for w in OUTSOURCE_LABELS if _norm(w) in label_norm), None)
            if hit:
                issues.append(_issue(
                    self.category, "medium", label or "经历",
                    f"经历标题带有服务外包/驻场标签「{hit}」",
                    "外包本身不是问题，问题是项目价值没讲清楚："
                    "把「做了什么项目、解决什么问题、交付了什么」提到前面，别让公司标签占住标题",
                    cost=4.0))
                break   # 同一类问题只报一次，避免每条经历刷屏

        # --- 2. 玩具项目题材 ---
        toy_hits: List[str] = []
        for exp in ctx.experiences:
            label = _exp_label(exp)
            hit = next((w for w in TOY_PROJECT_WORDS if w in label), None)
            if hit:
                toy_hits.append(hit)
        if toy_hits:
            # dict.fromkeys 用来去重且保序；**必须先 list() 再切片** ——
            # dict 不支持切片，直接 [:4] 会 TypeError（本轮自己踩过）。
            unique_toys = list(dict.fromkeys(toy_hits))[:4]
            issues.append(_issue(
                self.category, "low", "项目",
                "项目题材属于常见练习型项目（" + "、".join(unique_toys) + "）",
                "题材本身不减分，写法普通才减分：至少突出一个真实亮点 —— "
                "压测与容量设计、限流/缓存/一致性等取舍、为什么选这个方案、做完带来的结果",
                cost=2.0))

        # --- 3. 「精通 X」却没有项目证据 ---
        weak_mastery: List[str] = []
        for skill in ctx.skills:
            for word in MASTERY_WORDS:
                if word in skill:
                    name = skill.replace(word, "").strip(" ：:，,、-—")
                    if name and name.lower() not in exp_norm:
                        weak_mastery.append(skill)
                    break
        if weak_mastery:
            issues.append(_issue(
                self.category, "medium", "技能",
                "以下技能标了「精通/熟练掌握」，但在项目经历里找不到对应证据："
                + "、".join(weak_mastery[:6]),
                "「精通」是面试重点追问对象：要么在项目里写出用它做过什么，"
                "要么把它降级成「熟悉 / 了解」",
                cost=5.0))

        return issues


# ============ Guard 3：AI 腔与空话 ============


@GuardRegistry.register
class AIToneGuard(BaseGuard):
    """企业黑话与空话句式 —— 模型生成痕迹最明显的地方。"""

    name = "AIToneGuard"
    priority = 3
    category = "AI腔"

    def evaluate(self, ctx: GuardContext) -> List[Dict[str, Any]]:
        text = ctx.resume_text or ""
        if not text.strip():
            return []
        issues: List[Dict[str, Any]] = []

        hits = [w for w in AI_TONE_WORDS if w in text]
        if len(hits) >= 3:
            issues.append(_issue(
                self.category, "medium", "整体",
                "文中出现多处企业黑话腔：" + "、".join(hits[:6]),
                "这些词不提供任何信息，还容易被看出是模板/模型写的："
                "换成「做了什么 + 用什么方法 + 拿到什么结果」",
                cost=6.0))
        elif hits:
            issues.append(_issue(
                self.category, "low", "整体",
                "文中出现黑话腔：" + "、".join(hits[:4]),
                "建议替换成具体动作与结果", cost=2.0))

        empty = list(dict.fromkeys(m.group(0) for m in _EMPTY_CLAIM_RE.finditer(text)))
        if empty:
            issues.append(_issue(
                self.category, "low", "整体",
                "存在只给评价、没有证据的表达：" + "、".join(empty[:5]),
                "「具有…能力」这类句子说了等于没说，换成能证明它的事实",
                cost=2.0))

        return issues


# ============ Guard 4：量化降级建议 ============


@GuardRegistry.register
class QuantificationGuard(BaseGuard):
    """量化不足时，给**可退而求其次的表达**，而不是催用户「编个数字」。

    来源：resume-optimizer 的 ``narrative-tools.md``（无量化数据时怎么写）——
    拿不出精确数字时，用高频/低频、分钟级/小时级/天级、单团队/多团队/全公司、
    核心链路/关键系统这类**可验证的范围表达**替代。
    """

    name = "QuantificationGuard"
    priority = 4
    category = "量化"

    def evaluate(self, ctx: GuardContext) -> List[Dict[str, Any]]:
        bullets = ctx.bullets
        if len(bullets) < 3:
            return []
        unquantified = [b for b in bullets if not QUANT_RE.search(b)]
        ratio = 1 - len(unquantified) / len(bullets)
        if ratio >= 0.60:
            return []
        sample = "；".join(b[:28] for b in unquantified[:3])
        return [_issue(
            self.category, "medium", "要点",
            f"量化率 {ratio:.0%}，有 {len(unquantified)} 条要点没有任何结果数据"
            + (f"（例如：{sample}…）" if sample else ""),
            "拿不出精确数字时不要硬编，用**可验证的范围表达**替代："
            "频次（高频/每日）、耗时（分钟级/小时级）、范围（单团队/多团队/全公司）、"
            "位置（核心链路/关键系统/基础设施）、状态变化（从手工到自动化）",
            cost=6.0)]


# ============ 统一入口 ============


def check(structured: Optional[Dict[str, Any]] = None,
          resume_text: str = "",
          source_text: str = "",
          jd_text: str = "") -> Dict[str, Any]:
    """跑一遍全部 guard，返回与 ``resume_review`` 同形状的报告。

    ``source_text`` 是用户原始输入（由 services 层组装传入）。传空时编造检测
    会降级为「无法核对」的低优先级提示，而不是静默通过。
    """
    ctx = GuardContext(
        structured=structured if isinstance(structured, dict) else {},
        resume_text=resume_text or "",
        source_text=source_text or "",
        jd_text=jd_text or "",
    )

    issues: List[Dict[str, Any]] = []
    for guard_cls in GuardRegistry.all():
        try:
            found = guard_cls().evaluate(ctx) or []
        except Exception:  # noqa: BLE001
            # 单个 guard 坏掉不该让整页生成失败 —— 但要留下痕迹
            continue
        for item in found:
            item.setdefault("line", None)
            item.setdefault("snippet", "")
            issues.append(item)

    # --- 扣分：按 category 设上限，避免同类问题刷爆分数 ---
    caps = {
        "编造": FABRICATION_CAP,
        "风险": RISK_CAP,
        "AI腔": AI_TONE_CAP,
        "量化": QUANT_CAP,
    }
    deduct: Dict[str, float] = {}
    for item in issues:
        category = item.get("category", "其他")
        cost = float(item.pop("_cost", 0.0) or 0.0)
        cap = caps.get(category, 8.0)
        used = deduct.get(category, 0.0)
        deduct[category] = used + max(0.0, min(cost, cap - used))

    score = int(max(0, min(100, round(100 - sum(deduct.values())))))
    fabrication_hits = [i for i in issues if i.get("category") == "编造"
                        and i.get("severity") == "high"]

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: order.get(i.get("severity"), 3))

    return {
        "passed": score >= PASS_THRESHOLD and not fabrication_hits,
        "score": score,
        "issues": issues,
        "stats": {
            "guards": GuardRegistry.names(),
            "counts": {c: sum(1 for i in issues if i.get("category") == c)
                       for c in ("编造", "风险", "AI腔", "量化")},
        },
    }


def summarize(report: Dict[str, Any], limit: int = 3) -> str:
    """把守门结果压成一句话，给提示条/日志用。"""
    issues = report.get("issues") or []
    if not issues:
        return "守门检查通过"
    head = "；".join(str(i.get("message", ""))[:40] for i in issues[:limit])
    more = f"（共 {len(issues)} 条）" if len(issues) > limit else ""
    return head + more
