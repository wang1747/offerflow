"""简历结构化解析：把纯文本简历解析成「姓名/联系方式 + 分节」结构。

供导出排版使用。纯函数、确定性，不依赖 LLM。
"""

import re
from dataclasses import dataclass, field
from typing import List

from app.modules.resume.optimizer.sections import SECTION_HEADER


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:1[3-9]\d{9})|(?:\+?\d{2,4}[\s-]\d{7,8})")
_URL = re.compile(r"(?:https?://|github\.com/|www\.)", re.IGNORECASE)
_CONTACT_LABEL = re.compile(
    r"^(?:电话|手机|手机号|邮箱|电子邮箱|GitHub|地址|微信|求职意向|个人网站|博客)"
    r"\s*[:：]",
    re.IGNORECASE,
)
_TITLE_LINES = {"个人简历", "简历", "个人信息"}


@dataclass
class ResumeSection:
    title: str
    lines: List[str] = field(default_factory=list)


@dataclass
class StructuredResume:
    name: str = ""
    contact: List[str] = field(default_factory=list)
    sections: List[ResumeSection] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.name or self.contact or self.sections)


def _clean_line(line: str) -> str:
    return line.strip()


def _is_contact_line(line: str) -> bool:
    return bool(
        _EMAIL.search(line)
        or _PHONE.search(line)
        or _URL.search(line)
        or _CONTACT_LABEL.match(line)
    )


def _extract_name(line: str) -> str:
    match = re.search(r"姓名\s*[:：]\s*([^\s|｜,，;；]+)", line)
    return match.group(1).strip() if match else ""


def _looks_like_name(line: str) -> bool:
    if not line or line in _TITLE_LINES or _is_contact_line(line):
        return False
    if len(line) > 20 or re.search(r"[\d@:/\\]", line):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff·.\-A-Za-z ]{2,20}", line))


def parse_resume_structure(text: str) -> StructuredResume:
    """把纯文本简历解析为结构化简历。"""
    result = StructuredResume()
    lines = [_clean_line(line) for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return result

    # 1. 找到第一个分节标题
    first_section_idx = None
    for idx, line in enumerate(lines):
        if SECTION_HEADER.match(line):
            first_section_idx = idx
            break

    # 2. 头部：分节标题之前的内容 = 姓名 + 联系方式
    if first_section_idx is None:
        # 没有识别到分节，整份当作一个「正文」节
        header_lines = []
        body_lines = lines
    else:
        header_lines = lines[:first_section_idx]
        body_lines = lines[first_section_idx:]

    if header_lines:
        # 先识别显式「姓名：」，再从非联系方式行中找姓名。纯联系方式头部
        # 不再被误当成姓名（真实 WPS 简历常见：电话/邮箱/GitHub 三行置顶）。
        for line in header_lines:
            name = _extract_name(line)
            if name:
                result.name = name
                break
        if not result.name:
            for line in header_lines:
                if _looks_like_name(line):
                    result.name = line
                    break
        result.contact = [line for line in header_lines if line and line != result.name]
    elif body_lines and not SECTION_HEADER.match(body_lines[0]):
        # 没有头部时，只有第一行确实像姓名才消费它；不能把「教育经历」当姓名。
        if _extract_name(body_lines[0]):
            result.name = _extract_name(body_lines[0])
            body_lines = body_lines[1:]
        elif _looks_like_name(body_lines[0]):
            result.name = body_lines[0]
            body_lines = body_lines[1:]

    # 3. 分节
    current_title = "正文"
    current_lines: List[str] = []
    for line in body_lines:
        if SECTION_HEADER.match(line):
            if current_lines:
                result.sections.append(ResumeSection(title=current_title, lines=current_lines))
                current_lines = []
            current_title = re.sub(r"[:：]\s*$", "", line.strip())
        else:
            current_lines.append(line)
    if current_lines or (result.sections == [] and current_title != "正文"):
        result.sections.append(ResumeSection(title=current_title, lines=current_lines))

    # 兜底：没有分节且没有正文，至少给一个空节
    if not result.sections and not result.name:
        result.sections.append(ResumeSection(title="正文", lines=[]))

    return result
