"""结构化简历数据 -> 纯文本渲染。

渲染结果需要能被 `app.modules.resume.structure.parse_resume_structure` 识别，
因此第一行是姓名，联系方式紧跟其后，随后是已识别的分节标题。
"""

from typing import Any, Dict, List


TYPE_SECTION = {
    "project": "项目经历",
    "internship": "实习经历",
    "work": "工作经历",
}


def _line(value: Any) -> str:
    return str(value or "").strip()


def _contact_line(contact: Dict[str, Any]) -> str:
    if not isinstance(contact, dict):
        return ""
    parts = [
        _line(contact.get(key))
        for key in ("phone", "email", "github")
    ]
    return " | ".join(part for part in parts if part)


def _education_lines(education: List[Any]) -> List[str]:
    lines: List[str] = []
    for item in education:
        if not isinstance(item, dict):
            continue
        parts = [
            _line(item.get("school")),
            _line(item.get("major")),
            _line(item.get("degree")),
        ]
        main = " ".join(part for part in parts if part)
        start = _line(item.get("start"))
        end = _line(item.get("end"))
        if start and end:
            main = f"{main} {start} ~ {end}" if main else f"{start} ~ {end}"
        elif start:
            main = f"{main} {start}" if main else start
        elif end:
            main = f"{main} ~ {end}" if main else f"~ {end}"
        if main:
            lines.append(main)
        detail = _line(item.get("detail"))
        if detail:
            lines.append(detail)
    return lines


def _experience_section_title(item: Dict[str, Any]) -> str:
    kind = _line(item.get("type")) or "project"
    return TYPE_SECTION.get(kind, "经历")


def _experience_lines(experiences: List[Any]) -> List[str]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in experiences:
        if not isinstance(item, dict):
            continue
        title = _experience_section_title(item)
        groups.setdefault(title, []).append(item)

    lines: List[str] = []
    for title, items in groups.items():
        lines.append(title)
        for item in items:
            name = _line(item.get("name"))
            role = _line(item.get("role"))
            start = _line(item.get("start"))
            end = _line(item.get("end"))
            meta: List[str] = []
            if role:
                meta.append(role)
            if start and end:
                meta.append(f"{start} ~ {end}")
            elif start:
                meta.append(start)
            elif end:
                meta.append(end)
            header = name
            if meta:
                header = f"{name}（{'，'.join(meta)}）" if name else "（" + "，".join(meta) + "）"
            if header:
                lines.append(header)
            bullets = item.get("bullets") if isinstance(item.get("bullets"), list) else []
            for bullet in bullets:
                text = _line(bullet)
                if text:
                    lines.append("- " + text)
    return lines


def list_sections(data: Dict[str, Any]) -> List[str]:
    """返回渲染后出现的分节标题，供前端兼容展示。"""
    titles: List[str] = []
    if _line(data.get("summary")):
        titles.append("个人总结")
    if isinstance(data.get("education"), list) and data["education"]:
        titles.append("教育经历")
    seen: set = set()
    experiences = data.get("experiences") if isinstance(data.get("experiences"), list) else []
    for item in experiences:
        if isinstance(item, dict):
            title = _experience_section_title(item)
            if title not in seen:
                seen.add(title)
                titles.append(title)
    if isinstance(data.get("skills"), list) and data["skills"]:
        titles.append("技能")
    if isinstance(data.get("certifications"), list) and data["certifications"]:
        titles.append("证书")
    return titles


def render_resume_text(data: Dict[str, Any]) -> str:
    lines: List[str] = []

    name = _line(data.get("name"))
    if name:
        lines.append(name)

    contact = _contact_line(data.get("contact"))
    if contact:
        lines.append(contact)

    position = _line(data.get("position"))
    if position:
        lines.extend(["", "求职意向", position])

    summary = _line(data.get("summary"))
    if summary:
        lines.extend(["", "个人总结", summary])

    education = data.get("education") if isinstance(data.get("education"), list) else []
    if education:
        lines.append("")
        lines.append("教育经历")
        lines.extend(_education_lines(education))

    experiences = data.get("experiences") if isinstance(data.get("experiences"), list) else []
    if experiences:
        lines.append("")
        lines.extend(_experience_lines(experiences))

    skills = data.get("skills") if isinstance(data.get("skills"), list) else []
    skills = [_line(item) for item in skills]
    skills = [item for item in skills if item]
    if skills:
        lines.extend(["", "技能", "、".join(skills)])

    certifications = data.get("certifications") if isinstance(data.get("certifications"), list) else []
    certifications = [_line(item) for item in certifications]
    certifications = [item for item in certifications if item]
    if certifications:
        lines.extend(["", "证书", "、".join(certifications)])

    return "\n".join(lines).strip()
