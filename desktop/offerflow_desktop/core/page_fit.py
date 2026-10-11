"""一页约束：让 structured 与 resume_text 始终「同一份内容」
（自服务端 `modules/resume_generation/page_fit.py` 移植）。

## 要解决的问题

生成链路原本是「structured → 渲染文本 → 超长就用 LLM 精简**文本**」，
精简结果只写回 ``resume_text``，**``structured`` 一个字节都没动**。
而导出（PDF/Word）优先用 structured，
于是出现最坏的情况：**界面显示一页、导出却是两页**。

正确链路（本模块补的就是中间这一步）::

    structured → 渲染文本 → 检测超长 → 三刀精简（LLM 决定"删什么"）
      → 从精简文本重新提取 structured（本模块）
      → 重新渲染文本（此时 text 必然由最终 structured 生成）

## 设计取舍

1. **精简只由 LLM 决定删什么，落到 structured 上的动作是确定性的文本对齐**。
   不额外调一次 LLM 去"重新提取"。
2. **删减保守，绝不静默丢事实**：纯事实（姓名/联系方式/学校/专业/学历/时间/公司名/角色）
   **一律不删**；个人总结、要点按相似度对齐；技能/证书只做交集（只减不增，顺带防编造）。
   - **要点只在"自己那段经历"的文本块内匹配**：全池匹配会让 A 段把 B 段的要点认领过来，
     那是比"删多了"更糟的**数据污染**。
3. **退化守卫**：有要点却一条都没对上时，整体回退到精简前的 structured。

纯函数、零 LLM、零 IO，可直接单测。

⚠️ 两份同源声明
---------------
服务端同一份在 ``backend/app/modules/resume_generation/page_fit.py``，**改动必须两边一起改**。

⚠️ 但有几处**必须按桌面 render 对齐，不能照抄服务端**（否则静默失效）：

| 点 | 服务端 | 桌面（本文件） |
|---|---|---|
| 经历标题写法 | ``名字（角色，2025.03 ~ 2026.06）`` | ``名字（角色）｜2025.03 – 2026.06`` |
| ``_estimate_too_long`` 来源 | ``optimizer/service.py`` | 本文件内联（与 `services/resume_optimizer` 同一判据） |

桌面 render（`services/resume_generation._experience_lines`）用的是 ``｜`` 分隔的写法，
若这里照抄服务端的「逗号 + ~」，``_candidates_for`` 的相似度会低于阈值
⇒ 静默退化成全池匹配 ⇒ **数据污染**。所以 ``header_text()`` 逐字对齐桌面 render。
"""

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

from .resume_review import normalize_period

# 与桌面 render 的 bullet 前缀保持一致（render 只用 "- "，其余为容错 LLM 输出）
_BULLET_PREFIXES: Tuple[str, ...] = ("- ", "• ", "* ", "· ")

# 相似度阈值：取"偏保守"的一侧。
# 宁可多留（结果仍超长 → 报错让用户自己删），也不误删（简历莫名少了内容是致命问题）。
_MATCH_THRESHOLD = 0.60

_SUMMARY_TITLE = "个人总结"

# ===== 一页约束参数（与 `services/resume_optimizer` 同一组判据）=====
MAX_EXPAND_RATIO = 1.15
ONE_PAGE_MAX_CHARS = 1400
MIN_CONDENSE_CHARS = 800

# 分节标题（与 `core.resume_review.SECTION_ALIASES` / 桌面 render 的输出对齐）
SECTION_HEADER = re.compile(
    r"^\s*(?:教育|教育背景|教育经历|项目|项目经历|实习|实习经历|工作|工作经历|"
    r"经历|技能|专业技能|自我评价|个人评价|荣誉|获奖|证书|个人信息|基本资料|求职意向|"
    r"个人总结|个人简介|专业技能|掌握技能)"
    r"[^：:]{0,12}[:：]?\s*$"
)


def estimate_too_long(original: str, optimized: str) -> bool:
    """估算结果是否超出一页 A4（保守估计，用字符数）。

    与 ``services/resume_optimizer._estimate_too_long`` **同一判据**
    （那是服务端 ``optimizer/service.py`` 的同名函数；桌面侧先内联在这里，
    避免 core 依赖 services —— 复活优化链路时应让 services 反过来用本函数）。
    """
    o_len = len(original or "")
    n_len = len(optimized or "")
    # 明显扩写：比原文长 15% 以上，且绝对长度不短
    if o_len and n_len > o_len * MAX_EXPAND_RATIO and n_len > MIN_CONDENSE_CHARS:
        return True
    # 绝对超一页：超过字符数上限
    return n_len > ONE_PAGE_MAX_CHARS


def _norm(text: str) -> str:
    """归一化：去空白、去标点、小写。只用于比较，不改动原文。"""
    return re.sub(
        r"[\s\u3000，,。.；;：:、！!?？—–\-·•*\"'“”‘’()（）\[\]【】/\\|~]",
        "",
        (text or "").lower(),
    )


def _ratio(a: str, b: str) -> float:
    """两段文本的相似度（0~1）。包含关系直接给 0.9，避免被长度差稀释。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def split_lines(text: str) -> Tuple[List[str], List[str]]:
    """切出 (要点行正文, 非要点行)。与 render_resume_text 的输出格式对应。"""
    bullets: List[str] = []
    others: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        body = None
        for prefix in _BULLET_PREFIXES:
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                break
        if body:
            bullets.append(body)
        else:
            others.append(line)
    return bullets, others


def extract_section(text: str, title: str) -> Optional[str]:
    """取出「标题行 → 下一个标题行之间」的内容（多行用空格连接）；没有该分节返回 None。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    start = None
    for index, line in enumerate(lines):
        if line.rstrip(":：") == title:
            start = index + 1
            break
    if start is None:
        return None

    body: List[str] = []
    for line in lines[start:]:
        if SECTION_HEADER.match(line):
            break
        body.append(line)
    return " ".join(body)


def match_bullets(
    base_bullets: List[str],
    candidates: List[str],
    used: Optional[set] = None,
) -> Dict[int, str]:
    """把精简后的要点行对齐回原要点：返回 {原始下标: 采用的新写法}。

    - 逐条按"最相似且尚未被占用"的候选匹配；
    - **``used`` 必须由调用方跨经历共享**：否则两段经历会各自抢走同一行精简结果；
    - 相似度不达阈值 → 视为被 LLM 删除，不出现在返回值里；
    - 命中的候选**比原文更长**时沿用原文（LLM 没缩短它，就别引入多余的合并句）。
    """
    used = used if used is not None else set()
    result: Dict[int, str] = {}
    for index, bullet in enumerate(base_bullets):
        best_index, best_ratio = -1, 0.0
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index in used:
                continue
            ratio = _ratio(bullet, candidate)
            if ratio > best_ratio:
                best_index, best_ratio = candidate_index, ratio
        if best_index < 0 or best_ratio < _MATCH_THRESHOLD:
            continue
        used.add(best_index)
        candidate = candidates[best_index]
        result[index] = candidate if len(_norm(candidate)) <= len(_norm(bullet)) else bullet
    return result


def keep_short_texts(items: List[str], haystack: str) -> List[str]:
    """技能 / 证书这类短语：只在精简文本里还能找到时保留（只减不增，防编造）。"""
    haystack_norm = _norm(haystack)
    kept: List[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if not text:
            continue
        if _norm(text) and _norm(text) in haystack_norm:
            kept.append(text)
    return kept


def split_blocks(text: str) -> List[Tuple[str, List[str]]]:
    """把简历文本切成 ``[(标题行, 该标题下的要点列表)]``。

    切法与 render_resume_text 的输出格式对应：**非要点行开一个新块**，
    紧随其后的要点行归入该块。分节标题（"项目经历"）会得到一个空要点块，
    真正的经历块是紧随其后的「名字（角色）｜期间」那行。
    """
    blocks: List[Tuple[str, List[str]]] = []
    header: Optional[str] = None
    bullets: List[str] = []
    has_header = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        body = None
        for prefix in _BULLET_PREFIXES:
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                break
        if body:
            bullets.append(body)
            continue
        if has_header or bullets:
            blocks.append((header or "", bullets))
        header, bullets, has_header = line, [], True
    if has_header or bullets:
        blocks.append((header or "", bullets))
    return blocks


def header_text(experience: Dict[str, Any]) -> str:
    """与桌面 render 的经历标题行**保持同一写法**，供块匹配使用。

    ⚠️ 必须与 ``services/resume_generation._experience_lines`` 逐字对齐：
    ``名字（角色）｜2025.03 – 2026.06``。照抄服务端的「逗号 + ~」写法会让
    相似度掉到阈值以下，静默退化成全池匹配（数据污染）。
    """
    name = str(experience.get("name") or "").strip()
    role = str(experience.get("role") or "").strip()
    start = str(experience.get("start") or "").strip()
    end = str(experience.get("end") or "").strip()
    period = normalize_period(start, end or ("至今" if start else "")) if (start or end) else ""
    header = f"{name}（{role}）" if (name and role) else (name or (f"（{role}）" if role else ""))
    return f"{header}｜{period}" if (header and period) else (header or period)


def _candidates_for(
    experience: Dict[str, Any],
    blocks: List[Tuple[str, List[str]]],
    condensed_norm: str,
) -> Tuple[Optional[int], List[str]]:
    """定位这段经历在精简文本里的块，返回 ``(池标识, 它自己的要点行)``。

    **必须在块内匹配**：要点长得都很像（"负责 A 模块…"vs"负责 B 模块…"），
    一旦全池匹配，A 段经历就会把 B 段的要点认领过来 —— 那是比"删多了"更糟的数据污染。
    块标题对不上、但名字确实还在全文里时，退化为全池匹配（保守起见不轻易删内容）。

    **池标识的用处**：``match_bullets`` 的 ``used`` 只能在**同一个池内**共享 ——
    下标只在同一个池里有意义。所以块池返回块下标，全池退化返回 ``-1``。
    无候选时返回 ``(None, [])``，调用方不传 ``used``。
    """
    header = header_text(experience)
    best: Optional[List[str]] = None
    best_key: Optional[int] = None
    best_ratio = 0.0
    for idx, (block_header, block_bullets) in enumerate(blocks):
        if not block_header:
            continue
        ratio = _ratio(header, block_header)
        if ratio > best_ratio:
            best, best_ratio, best_key = block_bullets, ratio, idx
    if best is not None and best_ratio >= _MATCH_THRESHOLD:
        return best_key, best

    name = str(experience.get("name") or "").strip()
    if name and _norm(name) in condensed_norm:
        return -1, [bullet for _header, bullets in blocks for bullet in bullets]
    return None, []


def restructure_from_text(base: Dict[str, Any], condensed_text: str) -> Dict[str, Any]:
    """用精简后的文本重建 structured；重建不出来就原样返回 base。

    base 的事实层字段（姓名/联系方式/求职意向/学校/专业/学历/时间/公司名/角色）
    **原样保留**，只让表达层（个人总结、要点、技能、证书）跟随精简结果变化。
    """
    if not isinstance(base, dict):
        return base
    condensed = (condensed_text or "").strip()
    if not condensed:
        return base

    blocks = split_blocks(condensed)
    condensed_norm = _norm(condensed)
    # 个人总结：能从精简文本里取出就整段采用（这就是"重新提取"）；
    # 取不到说明 LLM 没动它，沿用原文 —— 不静默删事实。
    summary_block = extract_section(condensed, _SUMMARY_TITLE)

    new = dict(base)
    new["summary"] = summary_block or base.get("summary") or ""

    rebuilt_experiences: List[Dict[str, Any]] = []
    base_bullet_total = 0
    kept_bullet_total = 0
    # 每个候选池一份"已被占用的行"：`match_bullets` 的契约要求跨经历共享，
    # 但**只能在同一个池内共享**（下标只在池内有意义）。
    used_by_pool: Dict[Any, set] = {}
    for experience in (base.get("experiences") or []):
        if not isinstance(experience, dict):
            continue
        base_bullets = [
            str(item).strip()
            for item in (experience.get("bullets") or [])
            if str(item).strip()
        ]
        base_bullet_total += len(base_bullets)
        pool_key, candidates = _candidates_for(experience, blocks, condensed_norm)
        used = used_by_pool.setdefault(pool_key, set()) if pool_key is not None else None
        mapping = match_bullets(base_bullets, candidates, used)
        kept = [mapping[i] for i in sorted(mapping)]
        kept_bullet_total += len(kept)

        name = str(experience.get("name") or "").strip()
        name_present = bool(name) and _norm(name) in condensed_norm
        # 整段经历移除的条件很严：名字在全文中消失，且它的要点一条都没留下
        if base_bullets and not kept and not name_present:
            continue

        item = dict(experience)
        item["bullets"] = kept
        rebuilt_experiences.append(item)

    # 退化守卫：本来有要点却一条都没对上（多半是精简输出不可用）→ 整体回退
    if base_bullet_total and kept_bullet_total == 0:
        return base

    new["experiences"] = rebuilt_experiences
    new["skills"] = keep_short_texts(list(base.get("skills") or []), condensed)
    new["certifications"] = keep_short_texts(list(base.get("certifications") or []), condensed)
    return new


def is_too_long(text: str) -> bool:
    """是否超出一页（与调度用的 ``estimate_too_long`` 同一判据，避免两套口径）。"""
    return estimate_too_long("", text or "")
