"""发展连续性评估：简历时间线是否连贯。

成熟简历产品都会检查「时间线」，因为 HR 的第一反应就是扫时间轴：
空窗期、重叠、倒挂、经历早于入学……这些比文笔更致命，却最容易被工具忽略。

本模块是纯确定性的（不调 LLM），输入解析后的简历块，输出：

* ``timeline``  —— 归一化后的时间轴（起止统一成「年.月」+ 月份序号，便于比较）
* ``issues``    —— 时间线问题（时间写反 / 空窗 / 重叠 / 经历早于入学 / 顺序乱 / 职责倒挂）
* ``gaps``      —— 空窗期明细
* ``progression`` —— 职责等级序列，用于判断经历是不是越写越弱

判定取向：**宁少报不误报**。只写年份的时间点（如「2025」）会带 ±6 个月
的不确定度，因此阈值会自动放宽，避免把正常写法判成空窗。
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from . import expression_bank

# 时间点/时间段解析
_POINT_RE = re.compile(r"((?:19|20)\d{2}(?:\s*[.\-/年]\s*\d{1,2})?(?:\s*月)?)")
_NOW_WORDS = {"至今", "今", "现在", "now", "present", "current", "在职"}

GAP_MONTHS = 7              # 有全职工作经历时的空窗阈值
STUDENT_GAP_MONTHS = 18     # 纯学生简历的阈值：项目/实习本身是离散的，间隔正常
OVERLAP_MONTHS = 3          # 同类经历允许的正常重叠
EDU_EARLY_SLACK = 3         # 经历早于入学多少个月算异常

_YM = Tuple[int, bool]      # (年*12+月序号, 是否只有年份)


def _now_ym() -> int:
    today = date.today()
    return today.year * 12 + (today.month - 1)


def parse_point(raw: str) -> Optional[Any]:
    """把单个时间点解析成 月份序号（int）；「至今」返回 "now"；无法解析返回 None。

    只写年份时按年中（6 月）估算，并用第二个返回值标记不确定性。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.lower() in _NOW_WORDS:
        return "now"
    match = re.search(r"(19|20)\d{2}", text)
    if not match:
        return None
    year = int(match.group(0))
    rest = text[match.end():]
    month_match = re.search(r"(\d{1,2})", rest)
    if month_match and 1 <= int(month_match.group(1)) <= 12:
        month = int(month_match.group(1))
    else:
        month = 6
    return year * 12 + (month - 1)


def _approx(raw: str) -> bool:
    """时间点是否只精确到年（无法判断具体月份）。"""
    text = (raw or "").strip()
    if text.lower() in _NOW_WORDS:
        return False
    match = re.search(r"(19|20)\d{2}", text)
    if not match:
        return True
    rest = text[match.end():]
    month_match = re.search(r"(\d{1,2})", rest)
    return not (month_match and 1 <= int(month_match.group(1)) <= 12)


def parse_period(period: str) -> Tuple[Optional[Any], Optional[Any], bool]:
    """解析时间段，返回 (起始, 结束, 是否含不确定端点)。

    结束为 "now" 表示「至今」；只有一个时间点时视作「起始点」。
    """
    text = (period or "").strip()
    if not text:
        return None, None, True
    points = _POINT_RE.findall(text)
    approx = _approx(text)
    if not points:
        return None, None, True
    start = parse_point(points[0])
    if len(points) >= 2:
        end = parse_point(points[1])
    elif any(w in text.lower() for w in _NOW_WORDS):
        end = "now"
    else:
        end = start
    return start, end, approx


def _resolve(end: Any) -> Optional[int]:
    if end == "now":
        return _now_ym()
    return end if isinstance(end, int) else None


def _fmt(ym: Optional[int]) -> str:
    if ym is None:
        return "—"
    return "%d.%02d" % (ym // 12, ym % 12 + 1)


def build_timeline(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把教育经历与项目/实习/工作经历统一成时间轴条目。"""
    items: List[Dict[str, Any]] = []

    for edu in doc.get("education") or []:
        if not isinstance(edu, dict):
            continue
        start, end, approx = parse_period(edu.get("period") or "")
        items.append({
            "kind": "education", "group": "教育",
            "label": edu.get("left") or "教育经历",
            "raw_period": edu.get("period") or "",
            "start": start, "end": end, "approx": approx,
            "resolved_end": _resolve(end),
        })

    for exp in doc.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        start, end, approx = parse_period(exp.get("period") or "")
        items.append({
            "kind": "experience", "group": exp.get("group") or "经历",
            "label": exp.get("left") or "经历",
            "raw_period": exp.get("period") or "",
            "start": start, "end": end, "approx": approx,
            "resolved_end": _resolve(end),
        })

    return items


def _month_span(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None or b is None:
        return None
    return b - a


def evaluate_continuity(doc: Dict[str, Any]) -> Dict[str, Any]:
    """评估时间线连续性。"""
    timeline = build_timeline(doc)
    issues: List[Dict[str, str]] = []
    gaps: List[Dict[str, Any]] = []
    overlaps: List[Dict[str, Any]] = []
    deduct = 0.0

    def add(severity: str, location: str, message: str, fix: str, cost: float) -> None:
        nonlocal deduct
        deduct += cost
        issues.append({"severity": severity, "location": location,
                       "message": message, "fix": fix})

    dated = [t for t in timeline if t["start"] is not None]

    # --- 1. 时间写反 / 缺少时间 ---
    for item in dated:
        start, end = item["start"], _resolve(item["end"])
        if end is not None and end < start:
            add("high", item["label"],
                f"时间写反了：「{item['raw_period']}」的结束时间早于开始时间",
                "核对后改成「起始 – 结束」的自然顺序，HR 对时间线错误非常敏感", 6)

    missing = [t for t in timeline if t["start"] is None]
    for item in missing:
        if item["kind"] == "experience":
            add("low", item["label"], "这段经历没有写起止时间",
                "补上「2025.03 – 2025.08」这样的时间段，否则会被当成无效经历", 2)

    # --- 2. 经历早于入学 ---
    edu_items = [t for t in dated if t["kind"] == "education"]
    if edu_items:
        edu_start = min(t["start"] for t in edu_items)
        for item in dated:
            if item["kind"] == "experience" and item["start"] < edu_start - EDU_EARLY_SLACK:
                add("high", item["label"],
                    f"这段经历的开始时间（{_fmt(item['start'])}）早于入学时间（{_fmt(edu_start)}）",
                    "核对时间是否写错；若确实存在（如专升本/在职），请在经历里说明", 5)

    # --- 3. 空窗期（只看项目/实习/工作） ---
    # 阈值分档：有全职工作经历时按职场标准（7 个月）；
    # 纯学生简历放宽到 18 个月——校园项目本来就是一段一段做的，
    # 两段项目之间隔半年属于常态，按职场标准会大面积误报。
    work = sorted([t for t in dated if t["kind"] == "experience"],
                  key=lambda t: t["start"])
    base_gap = GAP_MONTHS if any(t["group"] == "工作经历" for t in work) else STUDENT_GAP_MONTHS
    for prev, curr in zip(work, work[1:]):
        prev_end = _resolve(prev["end"])
        if prev_end is None:
            continue
        gap = curr["start"] - prev_end
        slack = 6 if (prev["approx"] or curr["approx"]) else 0
        if gap > base_gap + slack:
            months = gap
            gaps.append({"after": prev["label"], "before": curr["label"],
                         "months": months, "from": _fmt(prev_end), "to": _fmt(curr["start"])})
            add("medium", f"{prev['label']} → {curr['label']}",
                f"两段经历之间有 {months} 个月空窗（{_fmt(prev_end)} → {_fmt(curr['start'])}）",
                "把空窗期的时间用在成长上也写清楚（自学/备考/开源项目），"
                "别留空白让 HR 自己猜", 3)

    # --- 4. 时间重叠（同类经历） ---
    for i in range(len(work)):
        for j in range(i + 1, len(work)):
            a, b = work[i], work[j]
            a_end, b_end = _resolve(a["end"]), _resolve(b["end"])
            if a_end is None or b_end is None:
                continue
            lo = max(a["start"], b["start"])
            hi = min(a_end, b_end)
            if hi - lo >= OVERLAP_MONTHS:
                overlaps.append({"a": a["label"], "b": b["label"], "months": hi - lo})
                add("medium", f"{a['label']} / {b['label']}",
                    f"两段经历时间重叠 {hi - lo} 个月（{_fmt(lo)} → {_fmt(hi)}）",
                    "若确为并行（如实习 + 校内项目），在标题里注明「同时」；否则核对时间", 3)

    # --- 5. 排序（每个分组内部应按时间倒序） ---
    # 注意：教育经历与项目/实习经历在简历里是**两个独立小节**，
    # 小节之间的先后不构成乱序，只有组内乱序才是问题。
    for kind, min_count, name in (("experience", 3, "经历"), ("education", 2, "教育经历")):
        group_starts = [t["start"] for t in timeline
                        if t["kind"] == kind and t["start"] is not None]
        if len(group_starts) >= min_count and group_starts != sorted(group_starts, reverse=True):
            add("low", f"{name}排序", f"{name}没有按时间倒序排列",
                "最近的排在最前面——HR 与 ATS 都是从第一条开始读", 2)

    # --- 6. 职责倒挂（越近的经历写得越弱） ---
    exps = sorted([t for t in dated if t["kind"] == "experience"],
                  key=lambda t: t["start"], reverse=True)
    levels: List[Dict[str, Any]] = []
    for exp in exps:
        text = exp["label"]
        for raw in doc.get("experiences") or []:
            if isinstance(raw, dict) and (raw.get("left") or "") == exp["label"]:
                text = " ".join([raw.get("left") or ""] + list(raw.get("bullets") or []))
                break
        levels.append({"label": exp["label"], "level": expression_bank.seniority(text)})

    reversed_progression = False
    if len(levels) >= 2:
        latest = levels[0]["level"]
        earlier = [lv["level"] for lv in levels[1:]]
        # 只提示「真倒挂」：最近一段本身写得被动（参与/协助级），
        # 而更早的经历明显更强。两段都已经用上「独立/主导」这类强表述时
        # 只是遣词强弱之别，不该算倒挂（否则每次都会误报一条）。
        if latest and latest <= 2 and max(earlier) >= latest + 2:
            reversed_progression = True
            add("low", levels[0]["label"],
                "最近一段经历的职责描述，明显弱于更早的经历",
                "把最近一段的「主导/独立/牵头」写出来，经历应该越写越强，而不是越写越弱", 2)
        # 全篇都是弱职责也提示一次
        if not any(lv["level"] >= 3 for lv in levels) and len(levels) >= 2:
            add("low", "经历整体", "所有经历的职责描述都偏被动（参与/协助级别）",
                "至少给最近一段加上「独立完成 / 主导 / 牵头」这类有主动性的表述", 2)

    issues.sort(key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(i["severity"], 3))
    score = int(max(0, min(100, round(100 - deduct))))
    return {
        "score": score,
        "issues": issues,
        "timeline": timeline,
        "gaps": gaps,
        "overlaps": overlaps,
        "progression": {"levels": levels, "reversed": reversed_progression},
        "dated_count": len(dated),
    }


def summarize(report: Dict[str, Any]) -> str:
    """一行摘要，供 UI 展示。"""
    report = report or {}
    issues = report.get("issues") or []
    if not issues:
        return "时间线连贯，未发现空窗或倒挂"
    parts = [f"{i['location']}：{i['message']}" for i in issues[:3]]
    more = f"（另有 {len(issues) - 3} 条）" if len(issues) > 3 else ""
    return "；".join(parts) + more
