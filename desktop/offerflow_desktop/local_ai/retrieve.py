"""P3 统一检索入口：所有「生成前先查库」都必须走这里。

**retrieve-then-generate 铁律**：桌面版任何 LLM 生成（简历生成 / 简历优化 /
面试题 / 模拟面试 / 薪资分析）在调用模型前，必须先经本模块取一次上下文，
不允许直接拼 prompt 就发模型。

数据来源优先级：

    1. 本地索引（local_ai/index.db，P2 建立，含 corpus + resumes + jd 三路）
       —— 离线可用、毫秒级、与服务器同模型同参数（向量逐位一致，已交叉验证）
    2. 云端语料库（services/cloud.py，HTTP 只读免登录）
       —— 仅在「本地索引未命中」且显式 fallback_cloud=True 时兜底；离线静默跳过

    另外叠加两块本地知识（P3b/P3c，都在本地、都不额外花模型调用）：
    · 记忆（local_ai/memory.py）    —— 关于用户本人的稳定事实，按 query 召回
    · 图谱（local_ai/graph.py）     —— 命中资料的 1 跳**跨资料**关系

为什么本地优先而不是云端优先：
    桌面版的目标是「AI 功能本地跑、离线可用」。本地索引就是从服务器拉全量
    后重算的，覆盖面与云端一致；把云端降为兜底，断网时生成质量不受影响。

返回结构（`retrieve_context`）：

    {
        "text":      str,      # 拼好的参考文本，直接塞进 prompt；空串=没查到
        "kind":      str,      # "local" / "cloud" / "memory" / ""（没命中）
        "hits":      int,      # 本地向量命中条目数
        "items":     [...],    # 本地命中时的明细（id/source/item_type/score）
        "backend":   str,      # "semantic" / "hash_fallback"（本次检索用的向量空间）
        "threshold": float,    # 本次实际生效的阈值（随 backend 变化）
        "error":     str,      # 仅在本地索引异常时非空（不阻断生成）
        "memory":    {...},    # 记忆块 {text, items, reason}
        "graph":     {...},    # 图谱块 {text, lines, reason}
    }
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 生成类调用默认取几条、截多长——与旧 cloud.retrieve_reference 的默认值对齐，
# 保证改造前后注入模型的参考体量相当。
DEFAULT_TOP_K = 3
DEFAULT_MAX_CHARS = 1200

# ---------------- 相似度阈值：必须与 backend 绑定（2026-09-30 实测修正） ----------------
#
# 【语义空间 · bge-small-zh-v1.5】实测分布：
#     相关（真实场景 query）  0.640 ~ 0.765
#     相关（裸岗位名短查询）  0.493 ~ 0.604
#     真噪声（库里确无内容）  ≤ 0.542   ← 上限在这
#   取 0.57：对噪声留 0.028 余量（旧值 0.55 只剩 0.008，几乎贴脸），
#   且真实场景召回零损失。**上限不可超 0.60** —— 裸查询"运营"真实命中仅 0.604，
#   再往上就会误杀正确内容。
SEMANTIC_MIN_SCORE = 0.57

# 【hash 词袋降级空间】embedder.embed_hash_fallback 产出的向量，余弦尺度整体
# 低于语义空间约 0.1~0.15：实测正样本仅 0.451 ~ 0.621、噪声 0.238 ~ 0.248。
# 若沿用 0.57，**正样本会被全部挡掉、检索静默变成"几乎总返回空"**（实测
# "AI技术岗" 0.540、"运营" 0.451 均被挡），用户会以为功能坏了。
# 故单独标定 0.35（正样本下限 0.451 与噪声上限 0.248 的中点）。
HASH_MIN_SCORE = 0.35

_HASH_SCALE = HASH_MIN_SCORE / SEMANTIC_MIN_SCORE  # ≈0.614

# ---------------- 回声防护：成果池不许「拿自己当参考」 ----------------
#
# 复用闭环把用户自己的生成/优化成果纳入了召回池，副作用是：**同一份底稿的历史版本**
# 与当前输入几乎一模一样，余弦会冲到 0.95+ 排在第一位，模型于是照抄自己的旧版 ——
# 生成结果和上一版没差别，用户看到的是「点了生成，内容没变」。
# 这类块对「写得更好」毫无贡献（内容一样，学不到新东西），直接丢弃。
# 只对成果池生效：corpus 范文不可能与用户输入同源（实测最高 0.765），不受影响。
ECHO_SCORE_CEILING = 0.95


def _is_work_source(src: Any) -> bool:
    """判断某个 source 是否属于「成果池」（从 index 取，避免两处各写一份常量）。"""
    from . import index as index_mod

    return str(src or "").strip().lower() in getattr(index_mod, "WORK_SOURCES", ())


def _threshold_for(backend: str, min_score: float) -> float:
    """按 backend 换算出本次生效阈值。

    调用方给的是**语义空间**阈值；降级到 hash 空间时按比例缩放，
    避免两个空间用同一个数字（尺度不同，必然错一边）。
    """
    if backend == "hash_fallback":
        return round(min_score * _HASH_SCALE, 3)
    return min_score


# ---------------- 记忆 / 图谱补充（P3b / P3c） ----------------
#
# 两者都是**纯本地 SQLite 读取**（记忆的召回不花模型调用；图谱只在建图时花过），
# 且都遵守同一条硬规则：**没有内容时完全 no-op** ——
#   记忆为空 → 连 embedding 模型都不加载（`memory._has_memories()` 的廉价判空）；
#   图谱为空 / 没建过 → 直接返回空块。
# 这条规则很重要：`retrieve_context` 是每一次生成的必经之路，
# 在这里多做一次 1~3s 的 ONNX 加载或一次无谓的 SQL 扫描，用户都能感觉到卡。
MEMORY_TOP_K = 3
MEMORY_MAX_CHARS = 500
GRAPH_MAX_LINES = 12
GRAPH_MAX_CHARS = 700


def _memory_block(query: str) -> Dict[str, Any]:
    """召回与本次 query 相关的「关于用户的已知事实」。失败一律吞掉。"""
    try:
        from . import memory

        items = memory.recall(query, top_k=MEMORY_TOP_K)
        return {"text": memory.format_block(items, max_chars=MEMORY_MAX_CHARS),
                "items": items, "reason": ""}
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆召回异常（不影响生成）：%s: %s", type(exc).__name__, exc)
        return {"text": "", "items": [], "reason": f"{type(exc).__name__}: {exc}"}


def _graph_block(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按命中资料做 1 跳**跨资料**关系展开。"""
    keys = [f"{h.get('source')}:{h.get('source_id')}" for h in hits
            if h.get("source") and h.get("source_id")]
    if not keys:
        return {"text": "", "lines": 0, "reason": "no_hits"}
    try:
        from . import graph

        res = graph.expand(keys, limit=GRAPH_MAX_LINES, max_chars=GRAPH_MAX_CHARS)
        return {"text": res.get("text") or "", "lines": res.get("lines", 0),
                "reason": res.get("reason", "")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("图谱展开异常（不影响生成）：%s: %s", type(exc).__name__, exc)
        return {"text": "", "lines": 0, "reason": f"{type(exc).__name__}: {exc}"}


def _local(query: str, item_type: Optional[str], top_k: int, max_chars: int,
           min_score: float, exclude_source: Optional[str],
           sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """查本地索引。返回统一结构；索引不可用时 text="" 且 error 有值。

    `sources` 是**白名单**（如 `["corpus", "generated", "optimized"]`），与
    `exclude_source` 二选一即可 —— 白名单形态天然排除了不在名单里的材料池。
    """
    from . import index as index_mod

    res = index_mod.search(query, top_k=top_k, item_type=item_type,
                           source=sources, exclude_source=exclude_source)
    if not res.get("ok"):
        err = str(res.get("error") or "本地索引不可用")
        logger.warning("本地索引不可用，本次不注入参考：%s", err)
        return {"text": "", "kind": "", "hits": 0, "items": [],
                "backend": "", "threshold": 0.0, "error": err, "echo_dropped": 0}

    backend = res.get("backend") or "semantic"
    thr = _threshold_for(backend, min_score)

    picked: List[Dict[str, Any]] = []
    used = 0
    echo_dropped = 0
    for r in res.get("results", []):
        score = float(r.get("score", 0))
        if score < thr:
            continue
        # 回声防护：成果池里命中「几乎就是自己」的块 → 它是当前底稿的历史版本，
        # 拿它当参考等于让模型照抄旧版（详见 ECHO_SCORE_CEILING 处注释）。
        if _is_work_source(r.get("source")) and score >= ECHO_SCORE_CEILING:
            echo_dropped += 1
            continue
        block = r.get("text") or ""
        if not block:
            continue
        # 放不下就整块跳过（**不做 slice 截断**）：截断会把最后一块切成半句
        # 喂给 LLM（实测 480 字被切成 276 字），语义不完整、反而误导模型。
        if used + len(block) > max_chars:
            break
        picked.append({
            "id": r.get("id"),
            # source_id 也要带上：图谱展开要靠 `{source}:{source_id}` 反查
            # 「这条命中来自哪份资料」（P3b）。只给 id 是不够的 —— id 是**块**主键
            # （多块时形如 `corpus:x#chunk2`），拼出来的 key 在图谱里查不到，
            # 表现为「明明命中了却一张关系都展不开」。
            "source": r.get("source"),
            "source_id": r.get("source_id"),
            "item_type": r.get("item_type"),
            "score": r.get("score"),
            "text": block,
        })
        used += len(block)
        if len(picked) >= top_k:
            break

    if not picked:
        return {"text": "", "kind": "", "hits": 0, "items": [],
                "backend": backend, "threshold": thr, "error": "",
                "echo_dropped": echo_dropped}

    body = "\n\n".join(p["text"] for p in picked)
    return {"text": body, "kind": "local", "hits": len(picked),
            "items": picked, "backend": backend, "threshold": thr, "error": "",
            "echo_dropped": echo_dropped}


def _cloud(query: str, item_type: Optional[str], top_k: int,
           max_chars: int) -> str:
    """查云端语料库（兜底）。失败静默返回空串。"""
    try:
        from ..services import cloud

        return cloud.retrieve_reference(
            query, item_type=item_type or "resume",
            top_k=top_k, max_chars=max_chars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("云端兜底检索失败：%s: %s", type(exc).__name__, exc)
        return ""


def retrieve_context(
    query: str,
    item_type: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_score: float = SEMANTIC_MIN_SCORE,
    fallback_cloud: bool = False,
    exclude_source: Optional[str] = None,
    sources: Optional[List[str]] = None,
    use_memory: bool = True,
    use_graph: bool = True,
) -> Dict[str, Any]:
    """生成前取参考上下文。本地索引优先，未命中（可选）用云端兜底。

    ## min_score 默认 0.57（按 backend 自动换算，见模块头）

    由实测分布定（2026-09-30，bge-small-zh-v1.5 512 维，全库 518 块）：

        | 类别                          | top1 范围     |
        |-------------------------------|---------------|
        | 相关·真实场景（岗位+技能等）    | 0.640 ~ 0.765 |
        | 相关·裸岗位名（"运营"等）       | 0.493 ~ 0.604 |
        | 真噪声（库里确无内容的近域查询）| ≤ 0.542       |
        | 远域无关（做饭/天气/体育…）     | ≤ 0.487       |

    0.57 对噪声留 0.028 余量；上限受裸查询约束不可超 0.60。
    **降级到 hash 词袋时自动换成 0.35** —— 该空间尺度整体低 0.1~0.15，
    沿用 0.57 会把正样本全部挡掉（实测 0.540 / 0.451）。

    ## exclude_source：生成简历时必须传 "resume"

    否则「我的简历」（source="resume"）会被用户自己的 query 检索回来
    （实测 score=**1.000**，因为 query 就是用户自己填的岗位+技能），
    把 top_k 第 1 位占掉 → 外部参考额度减半，严重时造成内容回声。
    传 "resume" 即排除它、只参考 corpus 语料。

    ## sources：来源白名单（复用闭环，2026-10-10）

    与 `exclude_source` 二选一。`sources=["corpus", "generated", "optimized"]`
    表示「共享范文 + 我自己的成果」—— 成果池是复用闭环的落点：用户历史生成/
    优化出来的优质稿成为后续同类生成的参考，这就是「越用越好用」。
    材料池（"resume"）不在白名单里，自引用防护由「不在名单」自然成立，
    此路径不必再传 exclude_source。
    成果池会**丢弃与输入几乎同分（>= 0.95）的块**：那是同一份底稿的历史版本，
    拿它当参考等于照抄旧版（回声防护，见 ECHO_SCORE_CEILING）。
    返回体的 `echo_dropped` 是本次被丢掉的回声块数，供诊断与取证。

    ## fallback_cloud 默认 **False**（实测后的修正）

    本地索引就是服务器语料的**完整副本**（P2 全量拉取 corpus 356 条），云端兜底
    几乎提供不了本地没有的内容，代价却有三个：
      1. 服务器 `/corpus/search` **无阈值过滤**——实测无关查询（"红烧肉的做法"）
         本地被正确滤掉后，云端兜底又返回 2402 字塞进 prompt，等于白滤；
      2. 每次生成都要联网，与"离线可用"目标相悖，且在线/离线行为不一致；
      3. HTTP 往返（8s 超时）拖慢生成。

    服务器语料若更新，用**重建本地索引**解决（一次拉取），而不是每次生成都联网。
    保留该参数只为将来确有需要时显式开启。

    ## use_memory / use_graph（P3b / P3c，默认都开）

    在检索命中之外，再拼两段本地知识：

        【关于你的已知信息（本地记忆）】  记忆层召回的稳定事实（P3c）
        命中块                            向量检索结果（P2/P3a）
        【跨资料关系（本地图谱）】        命中资料的 1 跳跨文档关系（P3b）

    两块都**没有内容时完全 no-op**（记忆为空连模型都不加载、图谱未建直接返回空），
    所以默认开启不会给「没建过图 / 没记忆」的用户带来任何额外开销。
    需要严格复现旧行为时显式传 False（测试里用得到）。

    任何异常都不抛出——检索失败绝不能让生成流程挂掉。
    """
    q = (query or "").strip()
    if not q:
        return {"text": "", "kind": "", "hits": 0, "items": [],
                "backend": "", "threshold": 0.0, "error": "", "echo_dropped": 0,
                "memory": {"text": "", "items": []}, "graph": {"text": "", "lines": 0}}

    try:
        local = _local(q, item_type, top_k, max_chars, min_score, exclude_source,
                       sources=sources)
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地检索异常，本次不注入参考：%s: %s", type(exc).__name__, exc)
        local = {"text": "", "kind": "", "hits": 0, "items": [],
                 "backend": "", "threshold": 0.0,
                 "error": f"{type(exc).__name__}: {exc}", "echo_dropped": 0}

    mem = (_memory_block(q) if use_memory
           else {"text": "", "items": [], "reason": "disabled"})
    gr = (_graph_block(local.get("items") or []) if use_graph
          else {"text": "", "lines": 0, "reason": "disabled"})

    parts: List[str] = []
    if mem["text"]:
        parts.append("【关于你的已知信息（本地记忆）】\n" + mem["text"])

    cloud_used = False
    if local["text"]:
        parts.append(local["text"])
    elif fallback_cloud:
        text = _cloud(q, item_type, top_k, max_chars)
        if text:
            parts.append(text)
            cloud_used = True

    if gr["text"]:
        parts.append("【跨资料关系（本地图谱）】\n" + gr["text"])

    body = "\n\n".join(parts)
    if local["text"]:
        kind = "local"
    elif cloud_used:
        kind = "cloud"
    elif mem["text"]:
        kind = "memory"
    else:
        kind = ""

    return {
        "text": body,
        "kind": kind,
        "hits": local["hits"],
        "items": local["items"],
        "backend": local.get("backend", ""),
        "threshold": local.get("threshold", 0.0),
        "error": local.get("error", ""),
        "echo_dropped": local.get("echo_dropped", 0),
        "memory": mem,
        "graph": gr,
    }


def retrieve_text(query: str, item_type: Optional[str] = None,
                  top_k: int = DEFAULT_TOP_K,
                  max_chars: int = DEFAULT_MAX_CHARS,
                  fallback_cloud: bool = False,
                  exclude_source: Optional[str] = None,
                  sources: Optional[List[str]] = None,
                  use_memory: bool = True,
                  use_graph: bool = True) -> str:
    """`retrieve_context` 的纯文本快捷方式（三处调用点直接用它）。"""
    return retrieve_context(
        query, item_type=item_type, top_k=top_k,
        max_chars=max_chars, fallback_cloud=fallback_cloud,
        exclude_source=exclude_source, sources=sources,
        use_memory=use_memory, use_graph=use_graph,
    )["text"]


__all__ = [
    "retrieve_context",
    "retrieve_text",
    "DEFAULT_TOP_K",
    "DEFAULT_MAX_CHARS",
    "SEMANTIC_MIN_SCORE",
    "HASH_MIN_SCORE",
    "MEMORY_TOP_K",
    "MEMORY_MAX_CHARS",
    "GRAPH_MAX_LINES",
    "GRAPH_MAX_CHARS",
]
