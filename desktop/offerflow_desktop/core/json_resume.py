"""JSON Resume 互转（导出 / 导入）。

**为什么单独有这个模块**
JSON Resume（jsonresume.org）是简历的通用交换格式，有官方 JSON Schema（v1.0.0）
和一批现成工具（在线预览、主题渲染、其它平台的导入）。有了它，OfferFlow 里生成的
简历能拿出去用，别处的简历也能搬进来，不必再手打一遍。

**两条硬约束决定了本模块的形状**

1. **导出要能被「别人的工具」读懂** ⇒ 严格贴 schema：
   - 日期只能是 `YYYY` / `YYYY-MM` / `YYYY-MM-DD`（`iso8601` 的 pattern 只认这三种）；
   - `work` 和 `projects` 是**两个独立数组**，不能按「反正是经历」合并；
   - `skills` 是 `[{name, keywords}]` 而不是字符串数组。
   写文件时只输出非空字段 —— 空串放在 `url` 这类带 `format` 校验的字段上会让
   整份文件在别人的工具里判定为非法。

2. **导入的终点是一条纯文本** ⇒ `db.upload_resume()` 只接受 `raw_text`，服务端
   再自己解析。所以 `from_json_resume()` 之后还要 `to_plain_text()` 渲染一遍。

**为什么用 ``meta`` 存私有字段**
官方 schema 的顶层与各子对象都是 ``additionalProperties: true``，并且专门留了
``meta`` 给工具方用（"The schema version and any other tooling configuration
lives here"）。OfferFlow 有三样东西在标准里没有对应槽位：

* 一条经历是 **工作 / 实习 / 项目** —— 它决定这条进 `work[]` 还是 `projects[]`；
* 这些经历**交错的原始顺序** —— 拆进两个数组之后，顺序就没了；
* `end` 到底是 **「至今」** 还是 **「确实没填」** —— 两者的表现都是「没有 endDate」。

把这三样放进 ``meta.offerflow``，换来的是**往返无损**。而别人给的文件里没有
``meta.offerflow`` 时照样能导入（见 `_restore_experiences` 的降级分支）——
这是刻意的优雅降级，不是遗漏。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .resume_review import normalize_period

# 官方 schema 的版本与地址。地址写进导出文件的 ``$schema``，
# 便于别的工具知道「按哪个版本校验」。
SCHEMA_VERSION = "v1.0.0"
SCHEMA_URL = ("https://raw.githubusercontent.com/jsonresume/resume-schema/"
              "v1.0.0/schema.json")

# meta.offerflow 的自描述。version 是**本模块数据结构的版本**，与 schema 版本无关 ——
# 将来映射规则变了，靠它区分新旧文件。
FORMAT_TAG = "offerflow"
FORMAT_NUMBER = 1

# 「至今」的各种写法。JSON Resume 的 endDate 必须是 iso8601，没有对应值，
# 所以导出时表现为「没有 endDate」，靠 meta 与「确实没填」区分开。
_PRESENT_WORDS = frozenset({"至今", "今", "现在", "now", "present", "current"})

# 归一日期用：年、可选月、可选日。
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")

# 官方 schema 里 iso8601 的 pattern（原样抄自 schema.json 的 definitions.iso8601）。
# 自检用它而不是另写一套：两处一旦不一致，"自检通过"就成了假信号。
_ISO_RE = re.compile(r"^([1-2][0-9]{3}-[0-1][0-9]-[0-3][0-9]"
                     r"|[1-2][0-9]{3}-[0-1][0-9]|[1-2][0-9]{3})$")

# 官方 schema 顶层允许的全部键（`additionalProperties: false`，多一个就非法）。
_TOP_LEVEL_KEYS = frozenset({
    "$schema", "basics", "work", "volunteer", "education", "awards", "certificates",
    "publications", "skills", "languages", "interests", "references", "projects", "meta",
})

# 各分区里可能出现的日期字段名。
_DATE_FIELDS = ("startDate", "endDate", "date", "releaseDate")

# 联系方式的轻量体检。schema 给 email 标了 format，值不像样时宁可不输出这个字段。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# OfferFlow 的经历类型。与 `core/resume_review._TYPE_GROUP` 的键保持一致。
EXPERIENCE_KINDS: Tuple[str, ...] = ("work", "internship", "project")
DEFAULT_KIND = "project"


# ============ 小工具 ============


def _text(value: Any) -> str:
    """取字符串。容器一律当空 —— 把 dict 直接 str() 出来会污染输出。"""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _put(target: Dict[str, Any], key: str, value: Any) -> None:
    """仅在值非空时写入。空字段**不出现**而不是出现成空串。"""
    text = _text(value)
    if text:
        target[key] = text


def _url(value: Any) -> str:
    """补全 scheme。schema 给 url 类字段标了 ``format: uri``，
    ``github.com/abc`` 这种写法在严格校验下过不去。"""
    text = _text(value)
    if not text:
        return ""
    if "://" not in text:
        return "https://" + text.lstrip("/")
    return text


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def normalize_date(value: Any) -> str:
    """把各种写法归一成 JSON Resume 认的 iso8601（``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``）。

    认不出来就返回空串 —— **绝不能返回原值**：schema 里 ``endDate`` 的 pattern
    只允许那三种形状，塞一个「至今」进去会让整份文件在别人的工具里判定为非法。
    「至今」的语义由调用方另行处理（见 `to_json_resume` 里的 ``open`` 标记）。

    >>> normalize_date("2023.09"), normalize_date("2023年9月"), normalize_date("2023")
    ('2023-09', '2023-09', '2023')
    >>> normalize_date("至今")
    ''
    """
    text = _text(value)
    if not text:
        return ""
    text = (text.replace("年", "-").replace("月", "-").replace("日", "")
                .replace("/", "-").replace(".", "-").strip("-"))
    match = _DATE_RE.match(text)
    if not match:
        return ""
    year, month, day = match.group(1), match.group(2), match.group(3)
    if month is None:
        return year
    result = "%s-%02d" % (year, int(month))
    if day is not None:
        result += "-%02d" % int(day)
    return result


def normalize_experience_kind(value: Any) -> str:
    """把经历类型规整成 work / internship / project。

    容忍中文（模型偶尔直接回「实习经历」）与未知值 —— 未知一律按项目处理，
    总比把一条经历丢掉强。
    """
    text = _text(value).lower()
    if text in EXPERIENCE_KINDS:
        return text
    if "实习" in text:
        return "internship"
    if "工作" in text or "全职" in text:
        return "work"
    if "项目" in text:
        return "project"
    return DEFAULT_KIND


def _is_present_word(value: Any) -> bool:
    return _text(value).lower() in _PRESENT_WORDS


# ============ 导出：OfferFlow → JSON Resume ============


def to_json_resume(structured: Dict[str, Any]) -> Dict[str, Any]:
    """OfferFlow 结构化简历 → JSON Resume 对象（可直接 `json.dumps`）。

    输出保证能被官方 schema 校验通过：空字段不出现、日期已归一、URL 已补 scheme。
    """
    data = structured if isinstance(structured, dict) else {}
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}

    basics: Dict[str, Any] = {}
    _put(basics, "name", data.get("name"))
    _put(basics, "label", data.get("position"))
    _put(basics, "summary", data.get("summary"))
    _put(basics, "phone", contact.get("phone"))
    email = _text(contact.get("email"))
    if email and _EMAIL_RE.match(email):
        basics["email"] = email
    github = _url(contact.get("github"))
    if github:
        basics["profiles"] = [{"network": "GitHub", "url": github}]

    # ---- 经历：按类型拆进 work[] / projects[]，顺序记进 meta ----
    work: List[Dict[str, Any]] = []
    projects: List[Dict[str, Any]] = []
    order: List[Dict[str, Any]] = []
    for item in _as_list(data.get("experiences")):
        if not isinstance(item, dict):
            continue
        kind = normalize_experience_kind(item.get("type"))
        raw_end = _text(item.get("end"))
        role = _text(item.get("role"))
        entry: Dict[str, Any] = {}
        _put(entry, "name", item.get("name"))
        start = normalize_date(item.get("start"))
        end = normalize_date(raw_end)
        if start:
            entry["startDate"] = start
        if end:
            entry["endDate"] = end
        bullets = [_text(b) for b in _as_list(item.get("bullets")) if _text(b)]
        if bullets:
            entry["highlights"] = bullets
        if kind == "work":
            if role:
                entry["position"] = role
        else:
            if role:
                entry["roles"] = [role]
            entry["type"] = kind
        if not entry:
            # 完全空的条目跳过：既没有名字也没有日期与要点，导出去只是一对空花括号。
            continue
        if kind == "work":
            work.append(entry)
        else:
            projects.append(entry)
        order.append({
            "src": "work" if kind == "work" else "projects",
            "i": (len(work) if kind == "work" else len(projects)) - 1,
            "kind": kind,
            "open": _is_present_word(raw_end),
        })

    # ---- 教育 ----
    education: List[Dict[str, Any]] = []
    for item in _as_list(data.get("education")):
        if not isinstance(item, dict):
            continue
        entry = {}
        _put(entry, "institution", item.get("school"))
        _put(entry, "area", item.get("major"))
        _put(entry, "studyType", item.get("degree"))
        start = normalize_date(item.get("start"))
        end = normalize_date(item.get("end"))
        if start:
            entry["startDate"] = start
        if end:
            entry["endDate"] = end
        detail = _text(item.get("detail"))
        if detail:
            # JSON Resume 的 education 没有「自由描述」字段，courses[] 是最近的一个槽。
            # **刻意不做 GPA 启发式提取**（把 "GPA 3.8" 猜成 score 再拼回来）：
            # 那会让往返结果依赖正则的运气，同一份数据导两次可能不一样。
            entry["courses"] = [detail]
        if entry:
            education.append(entry)

    # ---- 技能 ----
    skills: List[Dict[str, Any]] = []
    groups = data.get("skill_groups")
    if isinstance(groups, dict) and groups:
        for group_name, values in groups.items():
            items = [_text(v) for v in _as_list(values) if _text(v)]
            if items:
                skills.append({"name": _text(group_name) or "技能", "keywords": items})
    if not skills:
        # 没有分组时**每项单独成条**，刻意不包成 {"name": "技能", "keywords": [...]}：
        # 那样导入回来会多出一个名叫「技能」的分组，与原始的扁平列表不再一致。
        for name in _as_list(data.get("skills")):
            text = _text(name)
            if text:
                skills.append({"name": text})

    certificates = [{"name": _text(c)} for c in _as_list(data.get("certifications"))
                    if _text(c)]
    awards = [{"title": _text(h)} for h in _as_list(data.get("honors")) if _text(h)]

    offerflow: Dict[str, Any] = {"format": FORMAT_TAG, "version": FORMAT_NUMBER}
    if order:
        offerflow["experiences"] = order
    extra = [_text(x) for x in _as_list(data.get("extra")) if _text(x)]
    if extra:
        offerflow["extra"] = extra

    out: Dict[str, Any] = {"$schema": SCHEMA_URL, "basics": basics}
    if work:
        out["work"] = work
    if projects:
        out["projects"] = projects
    if education:
        out["education"] = education
    if skills:
        out["skills"] = skills
    if certificates:
        out["certificates"] = certificates
    if awards:
        out["awards"] = awards
    out["meta"] = {"version": SCHEMA_VERSION, "offerflow": offerflow}
    return out


def dumps(structured: Dict[str, Any]) -> str:
    """导出成 JSON 文本（尾随换行，便于命令行/编辑器查看）。"""
    return json.dumps(to_json_resume(structured), ensure_ascii=False, indent=2) + "\n"


# ============ 导入：JSON Resume → OfferFlow ============


def _experience_from(item: Dict[str, Any], kind: Any, is_open: bool) -> Dict[str, Any]:
    """把一条 work/projects 条目还原成 OfferFlow 的经历。"""
    kind = normalize_experience_kind(kind)
    role = ""
    if kind == "work":
        role = _text(item.get("position"))
    else:
        for candidate in _as_list(item.get("roles")):
            if _text(candidate):
                role = _text(candidate)
                break
        if not role:
            role = _text(item.get("position"))
    end = _text(item.get("endDate"))
    if not end and is_open:
        end = "至今"
    return {
        "name": _text(item.get("name")),
        "role": role,
        "type": kind,
        "start": _text(item.get("startDate")),
        "end": end,
        "bullets": [_text(b) for b in _as_list(item.get("highlights")) if _text(b)],
    }


def _restore_experiences(source: Dict[str, Any],
                         of_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """还原经历列表，顺序按 ``meta.offerflow.experiences``。

    没有 meta（别人的文件）时降级为「先 work 后 projects」—— 那个顺序无从得知，
    但至少不丢条目。
    """
    work = [x for x in _as_list(source.get("work")) if isinstance(x, dict)]
    projects = [x for x in _as_list(source.get("projects")) if isinstance(x, dict)]
    records = of_meta.get("experiences")
    out: List[Dict[str, Any]] = []
    if isinstance(records, list) and records:
        slots = {"work": work, "projects": projects}
        for record in records:
            if not isinstance(record, dict):
                continue
            bucket = slots.get(_text(record.get("src")))
            if not bucket:
                continue
            try:
                index = int(record.get("i"))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(bucket):
                continue
            out.append(_experience_from(bucket[index], record.get("kind"),
                                        bool(record.get("open"))))
        return out
    for item in work:
        out.append(_experience_from(item, "work", False))
    for item in projects:
        out.append(_experience_from(item, item.get("type"), False))
    return out


def _unmapped_lines(source: Dict[str, Any]) -> List[str]:
    """OfferFlow 没有对应分区的那些表（语言/志愿/发表/推荐人/兴趣）。

    汇入「其他」分区而不是静默丢弃 —— 用户从别处搬一份简历进来，
    发现少了两章会以为是我们弄坏了。
    """
    lines: List[str] = []
    for item in _as_list(source.get("volunteer")):
        if not isinstance(item, dict):
            continue
        parts = [_text(item.get("organization")), _text(item.get("position"))]
        parts = [p for p in parts if p]
        if parts:
            lines.append("志愿经历：" + " · ".join(parts))
    for item in _as_list(source.get("languages")):
        if not isinstance(item, dict):
            continue
        language, fluency = _text(item.get("language")), _text(item.get("fluency"))
        if language:
            lines.append("语言：%s" % (f"{language}（{fluency}）" if fluency else language))
    for item in _as_list(source.get("publications")):
        if not isinstance(item, dict):
            continue
        parts = [_text(item.get("name")), _text(item.get("publisher"))]
        parts = [p for p in parts if p]
        if parts:
            lines.append("发表：" + " · ".join(parts))
    for item in _as_list(source.get("references")):
        if not isinstance(item, dict):
            continue
        name, reference = _text(item.get("name")), _text(item.get("reference"))
        if name or reference:
            lines.append("推荐人：%s" % " · ".join(p for p in (name, reference) if p))
    for item in _as_list(source.get("interests")):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        keywords = [k for k in (_text(x) for x in _as_list(item.get("keywords"))) if k]
        if name or keywords:
            lines.append("兴趣：%s" % (f"{name}（{'、'.join(keywords)}）" if keywords else name))
    return lines


def from_json_resume(data: Dict[str, Any]) -> Dict[str, Any]:
    """JSON Resume 对象 → OfferFlow 结构化简历（与模型输出同形状）。

    返回的 dict 可以被 `to_json_resume` 逆向处理（往返无损），也可以交给
    `to_plain_text` 渲染成可上行的文本。
    """
    source = data if isinstance(data, dict) else {}
    basics = source.get("basics") if isinstance(source.get("basics"), dict) else {}
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    of_meta = meta.get("offerflow") if isinstance(meta.get("offerflow"), dict) else {}

    contact: Dict[str, str] = {}
    for key in ("phone", "email"):
        value = _text(basics.get(key))
        if value:
            contact[key] = value
    github = ""
    for profile in _as_list(basics.get("profiles")):
        if not isinstance(profile, dict):
            continue
        if _text(profile.get("network")).lower() == "github":
            github = _text(profile.get("url"))
            if github:
                break
    if not github:
        fallback = _text(basics.get("url"))
        if "github" in fallback.lower():
            github = fallback
    if github:
        contact["github"] = github

    education: List[Dict[str, Any]] = []
    for item in _as_list(source.get("education")):
        if not isinstance(item, dict):
            continue
        parts: List[str] = []
        score = _text(item.get("score"))
        if score:
            parts.append(score)
        parts.extend(_text(c) for c in _as_list(item.get("courses")) if _text(c))
        education.append({
            "school": _text(item.get("institution")),
            "major": _text(item.get("area")),
            "degree": _text(item.get("studyType")),
            "start": _text(item.get("startDate")),
            "end": _text(item.get("endDate")),
            "detail": "；".join(parts),
        })

    skills_flat: List[str] = []
    skill_groups: Dict[str, List[str]] = {}
    for item in _as_list(source.get("skills")):
        if isinstance(item, str):
            if _text(item):
                skills_flat.append(_text(item))
            continue
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        keywords = [_text(k) for k in _as_list(item.get("keywords")) if _text(k)]
        if keywords:
            skill_groups[name or "技能"] = keywords
            skills_flat.extend(keywords)
        elif name:
            skills_flat.append(name)

    certifications: List[str] = []
    for item in _as_list(source.get("certificates")):
        name = _text(item.get("name")) if isinstance(item, dict) else _text(item)
        if name:
            certifications.append(name)

    honors: List[str] = []
    for item in _as_list(source.get("awards")):
        if isinstance(item, dict):
            title = _text(item.get("title")) or _text(item.get("awarder"))
        else:
            title = _text(item)
        if title:
            honors.append(title)

    extra = [_text(x) for x in _as_list(of_meta.get("extra")) if _text(x)]
    extra.extend(_unmapped_lines(source))

    return {
        "name": _text(basics.get("name")),
        "contact": contact,
        "position": _text(basics.get("label")),
        "summary": _text(basics.get("summary")),
        "education": education,
        "experiences": _restore_experiences(source, of_meta),
        "skills": skills_flat,
        "skill_groups": skill_groups,
        "certifications": certifications,
        "honors": honors,
        "extra": extra,
    }


def loads(text: str) -> Dict[str, Any]:
    """解析 JSON Resume 文本。失败抛 ``ValueError``，消息可以直接展示给用户。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("文件是空的")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("不是合法的 JSON：第 %d 行第 %d 列 —— %s"
                         % (exc.lineno, exc.colno, exc.msg)) from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层应该是一个对象（{...}），实际是 %s"
                         % type(data).__name__)
    return data


def probe(data: Any) -> Tuple[bool, str]:
    """判断一个 JSON 对象像不像 JSON Resume；不像就说明原因。

    只在**一个字段都没有**时才判 False —— 一份简历可能只有 work 段而没有
    basics（从别处抠出来的片段），那也该让用户导进来看看。
    """
    if not isinstance(data, dict):
        return False, "顶层不是一个对象"
    known = ("basics", "work", "volunteer", "education", "awards", "certificates",
             "publications", "skills", "languages", "interests", "references",
             "projects", "meta")
    if not any(key in data for key in known):
        return False, ("这个 JSON 里没有任何 JSON Resume 字段"
                       "（basics / work / education / skills …），"
                       "可能不是简历文件")
    if "basics" not in data:
        return True, "文件里没有 basics 段，姓名与联系方式会是空的"
    return True, ""


# ============ 渲染成纯文本（导入路径的上行载体） ============


def _with_period(left: str, period: str) -> str:
    if not period:
        return left
    return ("%s    %s" % (left, period)).strip()


def _experience_left(item: Dict[str, Any]) -> str:
    name, role = _text(item.get("name")), _text(item.get("role"))
    if name and role:
        return f"{name}（{role}）"
    return name or role


def to_plain_text(structured: Dict[str, Any]) -> str:
    """OfferFlow 结构化简历 → 规范纯文本简历。

    **为什么必须有这一步**：`db.upload_resume()` 的入参只有 ``raw_text``，
    服务端再自己解析。所以 JSON 导入的终点是一条文本，而不是结构化对象。

    格式刻意贴 `core/resume_review` 的口径：分区标题用 `SECTION_ALIASES` 认识的字面、
    要点用 ``- `` 前缀、时间写成 ``2025.03 – 2026.06``。这样导入之后本地的质检引擎
    也能正确切分，而不只是「人看着像」。
    """
    data = structured if isinstance(structured, dict) else {}
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    lines: List[str] = []

    lines.append(_text(data.get("name")) or "（未填写姓名）")
    bits: List[str] = []
    for key in ("phone", "email"):
        value = _text(contact.get(key))
        if value:
            bits.append(value)
    github = _text(contact.get("github"))
    if github:
        bits.append("GitHub：" + github)
    if bits:
        # 第二行必须含手机号或邮箱，否则解析器不把这一行当联系方式
        # （见 resume_review 里对 head_idx[1] 的判定）。
        lines.append(" | ".join(bits))
    position = _text(data.get("position"))
    if position:
        # 标题单独一行、值放下一行。解析器按 ``startswith`` + 长度容差（len+4）
        # 匹配分区标题，写成「求职意向：AI 应用研发」这种行内形式长度必然超出容差，
        # 整行会退化成普通段落，求职意向就丢了（实测踩到）。
        lines.append("求职意向：")
        lines.append(position)

    summary = _text(data.get("summary"))
    if summary:
        lines.append("")
        lines.append("个人总结")
        lines.append(summary)

    education = [e for e in _as_list(data.get("education")) if isinstance(e, dict)]
    if education:
        lines.append("")
        lines.append("教育经历")
        for item in education:
            left = " ".join(x for x in (_text(item.get("school")), _text(item.get("major")),
                                        _text(item.get("degree"))) if x)
            detail = re.sub(r"\s+", " ", _text(item.get("detail"))).strip()
            if detail:
                # 解析器把「教育经历」下的**每一行**都当成一条新的学历（见主循环的
                # education 分支），detail 单独成行会凭空多出一条只有 GPA 的学历 ——
                # 实测踩到过。所以并进左列，用 · 分隔（不用括号：split_header 的
                # 清理规则会动括号，长文本还可能自带括号造成嵌套）。
                left = f"{left} · {detail}" if left else detail
            period = normalize_period(_text(item.get("start")), _text(item.get("end")))
            lines.append(_with_period(left, period))

    experiences = [e for e in _as_list(data.get("experiences")) if isinstance(e, dict)]
    if experiences:
        lines.append("")
        # 标题用「实践经历」而不是 UI 上的「实习/工作经历」：解析器按
        # ``SECTION_ALIASES`` 做 startswith 匹配，带斜杠的写法它认不出来
        # （`"实习/工作经历".startswith("实习经历")` 是 False），
        # 于是整个分区会退化成普通段落。「实践经历」在别名表里。
        lines.append("实践经历")
        for item in experiences:
            period = normalize_period(_text(item.get("start")), _text(item.get("end")))
            lines.append(_with_period(_experience_left(item), period))
            for bullet in _as_list(item.get("bullets")):
                text = _text(bullet)
                if text:
                    lines.append("- " + text)

    groups = data.get("skill_groups")
    flat = [_text(s) for s in _as_list(data.get("skills")) if _text(s)]
    if isinstance(groups, dict) and groups:
        lines.append("")
        lines.append("技能")
        for group_name, values in groups.items():
            items = [_text(v) for v in _as_list(values) if _text(v)]
            if items:
                lines.append(f"{_text(group_name) or '技能'}：{'、'.join(items)}")
    elif flat:
        lines.append("")
        lines.append("技能")
        lines.append("、".join(flat))

    for title, values in (("证书", data.get("certifications")),
                          ("荣誉奖项", data.get("honors")),
                          ("其他", data.get("extra"))):
        items = [_text(v) for v in _as_list(values) if _text(v)]
        if items:
            lines.append("")
            lines.append(title)
            lines.extend(items)

    return "\n".join(lines).strip() + "\n"


# ============ 导出后的结构自检 ============


@dataclass
class JsonVerifyResult:
    """JSON 导出的自检结果。

    与 `core.pdf_verify.PdfVerifyResult` **同形状**（鸭子类型 —— 见 `export_result`
    里 `export_message` 实际用到的成员）。这样 UI 的导出反馈只需要一套代码，
    不必为「PDF 回读」和「JSON 结构自检」写两份弹框逻辑。
    """

    ok: bool = True
    problems: List[str] = field(default_factory=list)

    @property
    def engine(self) -> str:
        return "内置结构自检"

    @property
    def checked(self) -> bool:
        """JSON 的自检永远是可做的 —— 不需要任何外部解析引擎。"""
        return True

    @property
    def degraded(self) -> bool:
        return False

    @property
    def note(self) -> str:
        return ""

    def summary(self) -> str:
        if self.ok:
            return "结构合法（顶层字段、日期格式、技能形状都符合 JSON Resume）"
        return "发现 %d 处不符合 JSON Resume 标准" % len(self.problems)

    def details(self, limit: int = 5) -> List[str]:
        return self.problems[:limit]


def _iter_dates(data: Dict[str, Any]):
    """遍历产物里所有日期字段，产出 ``(位置, 值)``。"""
    for key in ("work", "volunteer", "education", "projects", "awards",
                "certificates", "publications"):
        for index, item in enumerate(_as_list(data.get(key))):
            if not isinstance(item, dict):
                continue
            for name in _DATE_FIELDS:
                value = _text(item.get(name))
                if value:
                    yield "%s[%d].%s" % (key, index, name), value


def verify_export(data: Dict[str, Any]) -> JsonVerifyResult:
    """对**导出的对象**做一次轻量结构自检。

    为什么不用 jsonschema：桌面版不该为一个导出功能多背一个包，而这里要抓的三类
    问题（顶层多字段 / 日期不合法 / skills 形状错）十来行就能判准。**完整**的
    schema 校验放在仓库用例里用官方 `schema.json` 跑（`tests/test_json_resume.py`
    的外部判据部分），那里才是"别人的工具能不能读懂"的最终依据。

    与 PDF 那边同一个理念：导出成功 ≠ 内容正确。区别是 PDF 的失败模式是**静默丢字**，
    必须把文件读回来；JSON 的失败模式是**结构不合规**，直接查对象即可。
    """
    source = data if isinstance(data, dict) else {}
    problems: List[str] = []

    unknown = sorted(set(source) - _TOP_LEVEL_KEYS)
    if unknown:
        problems.append("顶层出现了 JSON Resume 不认识的字段：%s" % "、".join(unknown))

    for key in ("work", "volunteer", "education", "awards", "certificates",
                "publications", "skills", "languages", "interests", "references",
                "projects"):
        value = source.get(key)
        if value is not None and not isinstance(value, list):
            problems.append("%s 应该是数组，实际是 %s" % (key, type(value).__name__))

    for where, value in _iter_dates(source):
        if not _ISO_RE.match(value):
            problems.append("%s 的日期 %r 不是 YYYY / YYYY-MM / YYYY-MM-DD"
                            % (where, value))

    for index, item in enumerate(_as_list(source.get("skills"))):
        if not isinstance(item, dict) or not _text(item.get("name")):
            problems.append("skills[%d] 缺少 name（标准要求每项是 {name, keywords}）"
                            % index)

    basics = source.get("basics")
    if basics is not None and not isinstance(basics, dict):
        problems.append("basics 应该是对象")

    return JsonVerifyResult(ok=not problems, problems=problems)


__all__ = [
    "DEFAULT_KIND", "EXPERIENCE_KINDS", "FORMAT_NUMBER", "FORMAT_TAG",
    "JsonVerifyResult", "SCHEMA_URL", "SCHEMA_VERSION", "dumps", "from_json_resume",
    "loads", "normalize_date", "normalize_experience_kind", "probe",
    "to_json_resume", "to_plain_text", "verify_export",
]
