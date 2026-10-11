"""通用工具函数"""

import re
from typing import List


def extract_urls(text: str) -> List[str]:
    """从文本中提取 URL"""
    pattern = r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[^\s]*"
    return re.findall(pattern, text)


def truncate_text(text: str, max_length: int = 1000) -> str:
    """截断文本到指定长度"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."
