"""文本判重工具：全半角归一 + 分段指纹 + 双路相似度判据。

两处需要它：
- `modules/interview/services.py`：导入面经时判断是否与已有面经重复（长文本）；
- `agents/graphs/mock_interview.py`：模拟面试出题后判断是否与已问题目重复（短文本）。

为什么不能只用「整段相似度」：一份长面经补了几段再导入，整段相似度会掉到阈值以下
而漏判「重复导入同一份面经」；分段指纹按「较短一方的格子被覆盖多少」算，
补内容不影响它 —— 这正是「重复导入同一份面经」最常见的形态。
"""

import difflib
import re
import unicodedata


def dedup_normalize(text: str) -> str:
    """判重用的规范化文本：全半角归一 + 转小写 + 去掉所有空白与标点。

    旧版只 `re.sub(r"\\s+", "", ...)`：中文全角逗号、句号、括号、破折号都留着，
    同一份面经换个标点（`，` 写 `,`、`（）` 写 `()`）就能绕过查重，反复导入堆副本。
    `\\W` 在 Python 的 str 语义下是 Unicode 感知的 —— 汉字属字母类会被保留，
    去掉的正是标点与空白。
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"[\W_]+", "", normalized)


def shingles(text: str, size: int = 60, step: int = 30) -> set:
    """分段指纹：把规范化文本切成 size 字一格的滑动窗口集合。"""
    if len(text) <= size:
        return {text} if text else set()
    return {text[i:i + size] for i in range(0, len(text) - size + 1, step)}


def fingerprint_containment(a_norm: str, b_norm: str) -> float:
    """分段指纹包含度：较短一方的格子被覆盖的比例（0~1）。"""
    sa, sb = shingles(a_norm), shingles(b_norm)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def is_dup_pair(a_norm: str, b_norm: str, threshold: float = 0.9) -> bool:
    """两条**已规范化**的文本是否判为同一份内容。

    两路判据（任一路命中即重复）：
    1. 整段相似度 ≥ threshold（旧版判据，保留）；
    2. 分段指纹包含度 ≥ threshold —— 补了几段的长文本也能判出重复。
    """
    if not a_norm or not b_norm:
        return False
    if difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold:
        return True
    return fingerprint_containment(a_norm, b_norm) >= threshold


__all__ = ["dedup_normalize", "shingles", "fingerprint_containment", "is_dup_pair"]
