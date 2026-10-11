"""简历解析兼容入口，实际实现位于 resume 模块。"""

from app.modules.resume.parser import (
    parse_docx_bytes,
    parse_markdown,
    parse_markdown_text,
    parse_pdf,
    parse_pdf_bytes,
    parse_resume_bytes,
    parse_resume_file,
    parse_text,
)

__all__ = [
    "parse_docx_bytes",
    "parse_markdown",
    "parse_markdown_text",
    "parse_pdf",
    "parse_pdf_bytes",
    "parse_resume_bytes",
    "parse_resume_file",
    "parse_text",
]
