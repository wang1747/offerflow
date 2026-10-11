"""纯标准库语义相似度工具（中英混合），替代「纯字典命中」的退化匹配。

运行环境没有任何三方依赖（numpy / jieba / sklearn / sentence-transformers
一律不可用），所以这里自建轻量语义表示：

* ``tokens``    中英混合切词：ASCII 词元 + CJK 实词二元组（过滤虚词/招聘套话）
* ``coverage``  需求侧覆盖度：一条需求里有多少词元能在候选片段中找到
* ``similarity`` 双分支融合：两边都有 ASCII 技术词走「技术词命中 + 中文覆盖」，
  否则视为纯中文需求、直接看中文覆盖度
* ``shared_terms`` 给出可解释的公共词元，用于 UI 展示「凭什么判定相关」
* ``best_evidence`` 从一个需求出发，在若干候选片段里找最贴合的表述
* ``bm25``      词元级 BM25（对短文本池 min-max 归一化），用于排序

设计取向：**宁可保守，也不要虚假命中**。不相关的能力（Kafka / Kubernetes /
CUDA / Vue）必须稳稳落在 0，绝不允许因为「有个把汉字碰巧一样」就判为相关。
"""

import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------- 切词 ----------------

_ASCII_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#._-]{1,}")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

# 判定「相关但措辞不同」的阈值。
PARTIAL_THRESHOLD = 0.24

# 英文停用词（出现在任何 JD 里都没有区分度）
_EN_STOP = {
    "and", "or", "the", "a", "an", "of", "to", "in", "on", "for", "with",
    "is", "are", "be", "as", "at", "by", "from", "we", "you", "it", "that",
    "this", "will", "can", "have", "has", "our", "your", "their", "them",
    "job", "work", "team", "good", "able", "plus", "etc", "all", "any",
    "using", "used", "use", "new", "one", "two", "three", "not", "but",
    "web", "app", "http", "https", "sdk", "ide", "os", "ui", "ux", "db",
    "src", "lib", "doc", "docs", "demo", "test", "tests", "dev", "ops",
}

# CJK 高频虚词单字
_CJK_STOP_CHARS = set(
    "的了在与我你他她它们和及以及为对从到等这那有个是不也很就都而并或被把让使于其此该"
    "每上下前后中内外的得地着过们吗呢吧啊呀哦各多少能会要想还要"
)

# 整词级别的 CJK 停用二元组（招聘文案套话）
_CJK_STOP_BIGRAMS = {
    "熟悉", "了解", "掌握", "精通", "具备", "拥有", "具有", "能够", "可以",
    "需要", "必须", "要求", "负责", "参与", "相关", "经验", "能力", "工作",
    "岗位", "职位", "公司", "团队", "项目", "优先", "加分", "以上", "以下",
    "良好", "较强", "有较", "扎实", "熟练", "深入", "一定", "我们", "你们",
    "他们", "以及", "同时", "并且", "而且", "进行", "通过", "用于", "关于",
    "对于", "由于", "根据", "按照", "包括", "例如", "比如", "等等", "什么",
    "怎么", "如何", "是否", "一些", "一个", "一种", "这个", "那个",
}


def ascii_tokens(text: str) -> List[str]:
    """抽取 ASCII 词元（技术词基本都在这里）。"""
    return [t for t in _ASCII_TOKEN_RE.findall((text or "").lower()) if t not in _EN_STOP]


def cjk_bigrams(text: str) -> List[str]:
    """抽取 CJK 字符二元组，过滤虚词与招聘套话。"""
    chars = [c for c in (text or "") if _CJK_RE.match(c)]
    out: List[str] = []
    for i in range(len(chars) - 1):
        pair = chars[i] + chars[i + 1]
        if pair in _CJK_STOP_BIGRAMS:
            continue
        if chars[i] in _CJK_STOP_CHARS and chars[i + 1] in _CJK_STOP_CHARS:
            continue
        out.append(pair)
    return out


def tokens(text: str) -> List[str]:
    """中英混合词元序列：ASCII 词元 + CJK 实词二元组。"""
    return ascii_tokens(text) + cjk_bigrams(text)


# ---------------- 命中判定 ----------------

def _ascii_hit(term: str, pool: set) -> bool:
    """ASCII 技术词命中：允许「同词根写法差异」（postgresql ~ postgres）。"""
    if term in pool:
        return True
    if len(term) < 4:
        return False
    for other in pool:
        if len(other) < 4:
            continue
        if term in other or other in term:
            shorter, longer = sorted((term, other), key=len)
            if len(shorter) / len(longer) >= 0.6:
                return True
    return False


def coverage(requirement: str, chunk: str) -> Tuple[float, List[str]]:
    """需求侧覆盖度：需求里的词元有多少能在片段中找到。"""
    req = set(tokens(requirement))
    pool = set(tokens(chunk))
    if not req or not pool:
        return 0.0, []
    hits: List[str] = []
    for term in req:
        if term.isascii():
            if _ascii_hit(term, pool):
                hits.append(term)
        elif term in pool:
            hits.append(term)
    return len(hits) / float(len(req)), sorted(hits)


def similarity(a: str, b: str) -> float:
    """综合相似度 0~1（双分支）。"""
    if not (a or "").strip() or not (b or "").strip():
        return 0.0
    a_en = ascii_tokens(a)
    b_en = ascii_tokens(b)
    a_cjk = set(cjk_bigrams(a))
    b_cjk = set(cjk_bigrams(b))

    cjk_cov = (len(a_cjk & b_cjk) / float(len(a_cjk))) if a_cjk else 0.0

    if a_en and b_en:
        a_set, b_set = set(a_en), set(b_en)
        shared = sum(1 for t in a_set if _ascii_hit(t, b_set))
        ascii_signal = shared / float(len(a_set))
        return round(min(1.0, 0.65 * ascii_signal + 0.35 * cjk_cov), 4)
    return round(min(1.0, cjk_cov), 4)


def shared_terms(a: str, b: str, limit: int = 8) -> List[str]:
    """两边共享的可解释词元，ASCII 技术词优先。"""
    common = set(tokens(a)) & set(tokens(b))
    if not common:
        return []
    en = sorted(t for t in common if t.isascii())
    cn = sorted(t for t in common if not t.isascii())
    return (en + cn)[:limit]


# ---------------- BM25（用于池内排序） ----------------

def bm25(query: str, docs: Sequence[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    """对短文本池做词元级 BM25 打分，min-max 归一化到 0~1。"""
    q = tokens(query)
    if not q or not docs:
        return [0.0] * len(docs)
    corpus = [tokens(d) for d in docs]
    n = len(corpus)
    avgdl = sum(len(c) for c in corpus) / n if n else 0.0
    if avgdl <= 0:
        return [0.0] * n
    df = Counter()
    for c in corpus:
        df.update(set(c))

    raw: List[float] = []
    for c in corpus:
        tf = Counter(c)
        dl = len(c) or 1
        score = 0.0
        for term in set(q):
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * f * (k1 + 1.0) / (f + k1 * (1.0 - b + b * dl / avgdl))
        raw.append(score)

    hi, lo = max(raw), min(raw)
    if hi <= 0:
        return [0.0] * n
    span = hi - lo
    if span <= 1e-9:
        return [0.65 if s > 0 else 0.0 for s in raw]
    return [round((s - lo) / span, 4) for s in raw]


# ---------------- 需求 → 证据 ----------------

def best_evidence(requirement: str, chunks: Sequence[str],
                  threshold: float = 0.0) -> Tuple[float, str, List[str]]:
    """在若干候选片段里找最贴合需求的一条，返回 (分数, 片段, 公共词元)。"""
    best_score, best_chunk = 0.0, ""
    for chunk in chunks:
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        score = similarity(requirement, chunk)
        if score > best_score:
            best_score, best_chunk = score, chunk
    if best_score < threshold:
        return 0.0, "", []
    return best_score, best_chunk, shared_terms(requirement, best_chunk)


def explain(a: str, b: str) -> Dict[str, Any]:
    """调试/展示用：把各分量摊开。"""
    cov, hits = coverage(a, b)
    return {
        "similarity": similarity(a, b),
        "coverage": round(cov, 4),
        "hits": hits,
        "shared_terms": shared_terms(a, b),
    }
