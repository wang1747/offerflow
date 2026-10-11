"""社区内容审核：侮辱性 / 不当语言屏蔽。

发帖与评论在入口处检测，命中即拒绝，返回命中的词供日志记录。
词库为内置常见辱骂/攻击性用语，可在此增删扩展（后续可改为后台可配置）。
"""

import re
from typing import Optional

# 中文侮辱/攻击性用语：直接子串匹配（中文无需词边界）。
_CN_WORDS = [
    # 辱骂类
    "傻逼", "煞笔", "沙比", "蠢货", "笨蛋", "白痴", "弱智", "脑残",
    "废物", "垃圾人", "贱人", "贱货", "婊子", "骚货", "人渣",
    # 攻击类
    "去死", "该死", "不得好死", "滚蛋", "滚出去", "闭嘴吧",
    # 粗口类
    "他妈的", "你妈的", "妈的", "尼玛", "你麻痹", "卧槽", "我操", "操你",
    "狗东西", "狗日的",
    # 歧视/贬损类
    "神经病", "变态狂",
]

# 英文侮辱词：按词边界匹配，避免误伤（如 class 里的 ass、US B 里的 sb）。
_EN_WORDS = ["fuck", "shit", "bitch", "asshole", "bastard", "dickhead", "motherfucker"]


def _find_hit(text: str) -> Optional[str]:
    t = text.lower()
    for w in _CN_WORDS:
        if w in t:
            return w
    for w in _EN_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", t):
            return w
    return None


def moderate(text: str) -> Optional[str]:
    """检测文本是否含不当用语。命中返回命中的词，否则 None。"""
    if not text:
        return None
    return _find_hit((text or "").strip())


def moderate_fields(*parts: Optional[str]) -> Optional[str]:
    """检测多个字段，任一命中即返回命中的词。"""
    for p in parts:
        if not p:
            continue
        hit = moderate(p)
        if hit:
            return hit
    return None
