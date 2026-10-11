"""文档解析工具：支持 PDF、Markdown、HTML、TXT 文件"""

from typing import Optional

from bs4 import BeautifulSoup

from app.agents.tools.resume_parser import (
    parse_markdown_text,
    parse_pdf_bytes,
    parse_text,
)


def parse_pdf(file_bytes: bytes) -> str:
    """解析 PDF 文件为纯文本"""
    return parse_pdf_bytes(file_bytes)


def parse_image(file_path: str) -> Optional[str]:
    """OCR 识别图片文字（未内置 OCR 引擎，明确提示）"""
    raise ValueError("暂不支持图片 OCR，请粘贴文本或上传 PDF/Markdown 文件")


def parse_markdown(content: bytes) -> str:
    """解析 Markdown 字节流为纯文本"""
    return parse_markdown_text(content.decode("utf-8"))


def parse_html(content: bytes) -> str:
    """解析 HTML 字节流为纯文本"""
    soup = BeautifulSoup(content.decode("utf-8"), "html.parser")
    text = soup.get_text(separator="\n")
    return parse_text(text)


def parse_article_file(filename: str, content: bytes) -> str:
    """按扩展名自动解析面经文件"""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return parse_pdf(content)
    if name.endswith(".md") or name.endswith(".markdown"):
        return parse_markdown(content)
    if name.endswith(".html") or name.endswith(".htm"):
        return parse_html(content)
    if name.endswith(".txt"):
        return parse_text(content.decode("utf-8"))
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        raise ValueError("暂不支持图片 OCR，请粘贴文本或上传 PDF/Markdown 文件")
    raise ValueError(f"不支持的文件类型: {filename}")
