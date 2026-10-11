"""P2 索引编排：拉取 → 分块 → 本地 embedding → 写入 VectorStore → 检索。

分块参数与服务器 `app/modules/corpus/services.py` **完全一致**：
    _CHUNK_SIZE = 480 / _CHUNK_OVERLAP = 64
理由（服务器注释已写明）：bge-small-zh-v1.5 上下文上限 512 token，中文约 1 字≈1 token；
整篇 embedding 超出会被**静默截断**（512 字之后的内容不参与检索），所以必须切块。
沿同一套参数是为了本地索引与服务器索引在切块粒度上可比。

索引范围（三路进向量库，薪资不进）：
    corpus   → source="corpus"，item_type 沿用语料自身类型（resume/jd/interview）
    resumes  → source="resume"，item_type="resume"
    jd       → source="jd"，    item_type="jd"
    salary   → **不进向量库**（确定性规则表，走独立引擎）

对外入口：
    rebuild()        全量重建索引（先拉取，再重算；**并合并本地增量**）
    search()         语义检索（本地，离线可用）
    upsert_document() 单条文档增量入索引（用户新建/编辑后立刻可检索）
    remove_document() 单条文档出索引（删除后不再被检索命中）
    status()         索引与依赖现状

## 本地增量在哪（2026-09-30 补充）

「用户新建的数据」不需要额外落盘：**本地主存 `local_store.biz` 就是它的家**。
    · 离线新建的简历 / JD / 面经 → 先落 biz（state=pending）
    · rebuild() 时把 biz 里这些记录一并组装进索引（所以重建不会把它们抹掉）
    · 写入当下还会调 upsert_document() 增量入索引，让「刚建完就能检索」
判重规则（避免同一份数据被索引两遍、也避免用过期副本覆盖服务端版本）：
    某条本地记录 `_local_state == 'synced'`（服务端已确认）**且** 快照里已有同 id
    → 由快照提供（服务端版本为准）；否则（pending，或快照里没有）→ 用本地版本。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import embedder
from .paths import INDEX_DB_PATH
from .store import VectorStore

logger = logging.getLogger(__name__)

CHUNK_SIZE = 480       # 与服务器一致
CHUNK_OVERLAP = 64     # 与服务器一致
_EMBED_BATCH = 64

# 与服务器入库门槛一致：短于此长度的文本不进索引（否则噪声全进来）
MIN_TEXT = 20

# 本地实体 → (索引里的 source, item_type, 取文本的键)
#   source 沿用与「拉取快照」同一套命名（resume / jd），故检索侧的
#   source/exclude_source 过滤对本地增量同样生效（生成简历时排除 resume 的老规则不变）。
_LOCAL_ENTITIES: Tuple[Tuple[str, str, str, str], ...] = (
    ("resumes", "resume", "resume", "raw_text"),
    ("jds", "jd", "jd", "raw_text"),
    ("articles", "article", "interview", "raw_content"),
)

# ---------- 复用闭环：成果单独成池（2026-10-10） ----------
#
# 问题：生成/优化的产物流回本地后，简历实体里的 source 有 generated / optimized，
# 但索引一律按 ("resumes", "resume", ...) 写成 source="resume"，而生成时的检索
# 条件又带着 `exclude_source="resume"` —— 于是「把自己的成果排除掉」，
# 复用闭环从两端各断一次（实测：自有简历 score=0.6832 过阈值 0.57 仍被挡）。
#
# 做法：简历实体在入索引时**按记录自身的 source 分流**
#   · generated / optimized → 成果池：可被后续同类生成/优化召回（这就是「复用」）
#   · 其余（导入、上传）     → "resume" 材料池：保持被排除（防自引用，见 retrieve.py）
# 两个池的语义完全不同，用同一个 source 表达是这次断链的根因。
WORK_SOURCES: Tuple[str, ...] = ("generated", "optimized")

# 成果进「复用池」的质量闸门：不过关的**降级回材料池**（不参与召回）。
# 为什么降级而不是丢弃：本地主存里那条记录还在、列表里照样看得到，
# 只是不让它污染后续生成的参考 —— 拿一份没通过守门的稿子当范文是有害的。
WORKS_MIN_QUALITY = 60


def _quality_of(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从本地记录里取质量报告（生成/优化落库时写进 parsed_json.quality）。

    **空报告视同没有报告**（返回 None）：`save_generated` 在结果里没有质量报告时
    写下的正是 `{}`，若把 `{}` 当成「0 分的报告」，这类成果就会被判成「明确不合格」
    而遭降级 —— 与 `_work_quality_ok` 那句「没有报告就放行」自相矛盾。
    （2026-10-10 由反向对照 M11 暴露：把落库 payload 的质量报告抽成 `{}` 后，
    低质成果仍被挡住，用例因此分辨不出「闸门到底有没有拿到分数」。）
    """
    if not isinstance(item, dict):
        return None
    for holder in (item.get("parsed_json"), item):
        if isinstance(holder, dict):
            q = holder.get("quality")
            if isinstance(q, dict) and q:   # 非空才算「有报告」
                return q
    return None


def _is_work_source(source: str) -> bool:
    return str(source or "").strip().lower() in WORK_SOURCES


def _work_quality_ok(item: Dict[str, Any]) -> bool:
    """成果能否进复用池。

    有质量报告 → 按门槛判（passed 或总分 >= WORKS_MIN_QUALITY）；
    **没有质量报告的老成果 → 放行**：它确实是用户生成/优化的产物，是复用的对象，
    闸门只负责挡住「明确不合格」的，不该把历史数据一律判死。
    """
    q = _quality_of(item)
    if q is None:
        return True
    if q.get("passed"):
        return True
    try:
        return float(q.get("total_score") or 0) >= WORKS_MIN_QUALITY
    except (TypeError, ValueError):
        return True


def local_source_of(entity: str, item: Dict[str, Any], default: str) -> str:
    """决定一条本地记录**进索引时的 source**（重建与增量两条路径共用）。

    只有简历实体需要分流 —— JD / 面经没有「成果」概念。
    """
    if entity != "resumes":
        return default
    s = str((item or {}).get("source") or "").strip().lower()
    if _is_work_source(s):
        return s if _work_quality_ok(item) else "resume"
    return "resume"


def _collect_work_ids() -> Dict[str, set]:
    """本地主存里「已进成果池」的简历 id：{entity: {id}}。

    重建时必须先拿到它 —— 同一个 id 在服务端快照里也有（source 恒为 "resume"），
    不跳过就会**同一条被索引两遍**（一遍材料池、一遍成果池）。
    """
    from .. import local_store

    out: Dict[str, set] = {}
    try:
        for it in local_store.list_items("resumes"):
            if not isinstance(it, dict):
                continue
            sid = str(it.get("id") or "")
            if sid and _is_work_source(local_source_of("resumes", it, "resume")):
                out.setdefault("resumes", set()).add(sid)
    except Exception as exc:  # noqa: BLE001
        # 读不到本地主存（未登录 / 库损坏）不该让重建失败，最坏是成果这次没进池
        logger.warning("读取本地成果 id 失败：%s: %s", type(exc).__name__, exc)
    return out


INDEX_DB = INDEX_DB_PATH

_store: Optional[VectorStore] = None
_bg_lock = threading.Lock()
_bg_running = False


def _get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore(INDEX_DB)
    return _store


# ---------- 分块 ----------

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """把文本切成重叠块；<= size 时返回单元素列表（与服务器 _chunk_text 同行为）。"""
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= size:
        return [t]
    chunks: List[str] = []
    start = 0
    while True:
        chunks.append(t[start:start + size])
        if start + size >= len(t):
            break
        start += size - overlap
    return chunks


def _chunk_id(source: str, source_id: str, index: int, total: int) -> str:
    """单块 = {source}:{id}（与服务器的"不切块沿用裸 id"思路一致）；多块加 #chunkN。"""
    base = f"{source}:{source_id}"
    return base if total <= 1 else f"{base}#chunk{index}"


# ---------- 组装块记录 ----------

def _records_for(source: str, source_id: str, text: str, item_type: str,
                 created_at: Optional[str] = None) -> List[Dict[str, Any]]:
    """把「一份文档」切成待写入的块记录。快照与本地增量共用这一条路径，
    保证两边切块/主键规则完全一致（`_chunk_id` 相同 → 重复索引也只是覆盖，不会翻倍）。"""
    raw = (text or "").strip()
    if len(raw) < MIN_TEXT:
        return []
    sid = str(source_id or "")
    if not sid:
        return []
    pieces = chunk_text(raw)
    total = len(pieces)
    return [{
        "id": _chunk_id(source, sid, i, total),
        "source": source,
        "source_id": sid,
        "item_type": item_type,
        "chunk_index": i,
        "chunk_total": total,
        "text": piece,
        "created_at": created_at,
    } for i, piece in enumerate(pieces)]


def _records_from_snapshot(source: str, snap: Optional[Dict[str, Any]],
                           skip_ids: Optional[set] = None) -> List[Dict[str, Any]]:
    """把一份快照转成待写入的块记录。

    `skip_ids`：这些 id **不由快照出索引**（交给本地主存以成果来源入索引），
    避免同一条同时以 "resume" 和 "generated" 两个 source 各出一份。
    """
    if not snap:
        return []
    items = snap.get("items")
    if source == "salary":
        return []  # 规则表不进向量库
    if not isinstance(items, list):
        return []

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "")
        if not sid or (skip_ids and sid in skip_ids):
            continue
        if source == "corpus":
            src_name, item_type = "corpus", (it.get("item_type") or "")
        elif source == "resumes":
            src_name, item_type = "resume", "resume"
        else:
            src_name, item_type = "jd", "jd"
        out.extend(_records_for(src_name, sid, it.get("raw_text") or "", item_type,
                                it.get("created_at")))
    return out


def _local_records(covered: Dict[str, set]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把本地主存里「用户自己的数据」组装成块记录（rebuild 时合并进索引）。

    `covered`: {source: {source_id, ...}} —— 快照里已有的条目。
    规则见模块头「本地增量在哪」：已 synced 且快照已有 → 交给快照；否则用本地版本。
    """
    from .. import local_store

    out: List[Dict[str, Any]] = []
    stat: Dict[str, Any] = {}
    skipped = 0
    for entity, source, item_type, text_key in _LOCAL_ENTITIES:
        try:
            items = local_store.list_items(entity)
        except Exception as exc:  # noqa: BLE001
            # 本地主存读不出来（库损坏/未登录）不该让整次重建失败
            logger.warning("读取本地主存 %s 失败：%s: %s", entity, type(exc).__name__, exc)
            continue
        n_chunks = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            sid = str(it.get("id") or "")
            if not sid:
                continue
            # 按记录自身分流：成果 → generated/optimized 池（复用），其余 → 材料池。
            # **成果即使 synced 也不用让给快照** —— 快照里那条的 source 是 "resume"，
            # 让出去就等于把成果重新扔回被排除的材料池，复用池永远空。
            src = local_source_of(entity, it, source)
            if (not _is_work_source(src)
                    and it.get("_local_state") == "synced"
                    and sid in covered.get(source, set())):
                skipped += 1
                continue
            recs = _records_for(src, sid, it.get(text_key) or "", item_type,
                                it.get("created_at"))
            n_chunks += len(recs)
            out.extend(recs)
        stat[source] = {"items": len(items), "chunks": n_chunks}
    return out, {"sources": stat, "chunks": len(out),
                 "skipped_covered_by_snapshot": skipped}


# ---------- 重建索引 ----------

def rebuild(
    progress: Optional[Callable[[str, int, int], None]] = None,
    do_pull: bool = True,
) -> Dict[str, Any]:
    """全量重建本地索引。

    进度回调 progress(stage, done, total)：stage ∈ {"pull","chunk","embed"}。
    do_pull=False 时只用现有快照重建（离线可用）。

    **本地增量不会被抹掉**：索引内容 = 四路快照（corpus / resumes / jd）
    + 本地主存里用户自己的数据（含离线新建、尚未上行的）—— 见 `_local_records`。
    """
    from . import pull as pull_mod

    t0 = time.time()
    report: Dict[str, Any] = {"pull": None, "sources": {}, "local": None,
                              "chunks": 0, "embedded": 0}

    if do_pull:
        if progress:
            progress("pull", 0, len(pull_mod.SOURCES))
        report["pull"] = pull_mod.pull_all()

    # 组装块
    records: List[Dict[str, Any]] = []
    covered: Dict[str, set] = {}
    # 先取「本地成果 id」：这批在快照里也有（source 恒为 "resume"），必须跳过快照那份，
    # 改由本地以 generated/optimized 入索引 —— 否则同一条会出两份、且成果池恒空。
    work_ids = _collect_work_ids()
    report["work_items"] = sum(len(v) for v in work_ids.values())
    for name in ("corpus", "resumes", "jd"):
        snap = pull_mod.load_cache(name)
        recs = _records_from_snapshot(name, snap, skip_ids=work_ids.get(name))
        report["sources"][name] = {
            "items": (snap or {}).get("count", 0),
            "chunks": len(recs),
            "fetched_at": (snap or {}).get("fetched_at"),
        }
        records.extend(recs)
        # 记录「快照已覆盖」的 id（仅个人数据两路有意义；corpus 不在本地主存里）
        for r in recs:
            covered.setdefault(r["source"], set()).add(r["source_id"])

    # 本地增量（用户新建 / 离线编辑的数据）：
    # 不去另存一份副本，直接读本地主存 —— 所以重建**不会**把用户自己加的东西抹掉。
    # 追加在快照之后是有意的：万一同一条两边都有（pending 版本更新），本地版本胜出。
    local_recs, local_stat = _local_records(covered)
    records.extend(local_recs)
    report["local"] = local_stat

    report["chunks"] = len(records)
    if progress:
        progress("chunk", len(records), len(records))

    if not records:
        raise RuntimeError("没有任何可索引的文本（快照为空或内容过短）")

    # embedding
    backend = "semantic" if embedder.is_available() else "hash_fallback"
    texts = [r["text"] for r in records]
    vecs: List[List[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        batch = texts[i:i + _EMBED_BATCH]
        if backend == "semantic":
            vecs.extend(embedder.embed_texts(batch))
        else:
            vecs.extend(embedder.embed_hash_fallback(t) for t in batch)
        if progress:
            progress("embed", min(i + _EMBED_BATCH, len(texts)), len(texts))

    for r, v in zip(records, vecs):
        r["vec"] = v

    # 写库
    store = _get_store()
    store.clear()
    store.upsert_chunks(records)
    store.set_meta("built_at", datetime.now().isoformat(timespec="seconds"))
    store.set_meta("backend", backend)
    # 记录模型名：换模型后向量空间不同，种子索引与旧索引都必须作废重建
    store.set_meta("model", embedder.MODEL_NAME)

    report["embedded"] = len(vecs)
    report["backend"] = backend
    report["elapsed_ms"] = int((time.time() - t0) * 1000)
    return report


# ---------- 单条增量：用户新建 / 编辑后「立刻可检索」 ----------

def _embed_one_batch(backend: str, texts: List[str]) -> List[List[float]]:
    if backend == "hash_fallback":
        return [embedder.embed_hash_fallback(t) for t in texts]
    return embedder.embed_texts(texts)


def _write_document(source: str, source_id: str, text: str, item_type: str,
                    created_at: Optional[str], backend: str) -> Dict[str, Any]:
    store = _get_store()
    recs = _records_for(source, source_id, text, item_type, created_at)
    if not recs:
        return {"ok": False, "error": f"内容过短（< {MIN_TEXT} 字），未入索引"}
    vecs = _embed_one_batch(backend, [r["text"] for r in recs])
    for r, v in zip(recs, vecs):
        r["vec"] = v
    removed = store.delete_chunks(source, str(source_id))  # 先删旧块（文本变短不留残块）
    store.upsert_chunks(recs)
    # meta 补齐：检索端靠 meta.backend 决定阈值换算，缺了会按语义空间阈值去比 hash 向量
    if not store.get_meta("built_at"):
        store.set_meta("built_at", datetime.now().isoformat(timespec="seconds"))
    store.set_meta("backend", backend)
    store.set_meta("model", embedder.MODEL_NAME)
    return {"ok": True, "source": source, "source_id": str(source_id),
            "chunks": len(recs), "replaced_chunks": removed, "backend": backend}


_bg_pending: List[tuple] = []


def _bg_worker() -> None:
    """后台把待索引的文档写进索引（模型未加载时才走这条路）。"""
    global _bg_running
    while True:
        with _bg_lock:
            if not _bg_pending:
                _bg_running = False
                return
            args = _bg_pending.pop(0)
        try:
            if embedder.is_available():
                _write_document(*args)
            else:
                logger.warning("embedding 模型不可用，本地增量留待下次重建：%s:%s",
                               args[0], args[1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台增量索引失败（%s:%s）：%s: %s",
                           args[0], args[1], type(exc).__name__, exc)


def _spawn_background_index(source: str, source_id: str, text: str,
                            item_type: str, created_at: Optional[str],
                            backend: str) -> None:
    """模型尚未加载时**不阻塞调用方**（写入路径可能正跑在 UI 线程）：
    丢给后台线程串行处理；同一个文档重复入队也无害（先删后插，结果一致）。"""
    global _bg_running
    with _bg_lock:
        _bg_pending.append((source, source_id, text, item_type, created_at, backend))
        if _bg_running:
            return
        _bg_running = True
    threading.Thread(target=_bg_worker, name="offerflow-index-warm", daemon=True).start()


def upsert_document(source: str, source_id: str, text: str,
                    item_type: Optional[str] = None,
                    created_at: Optional[str] = None) -> Dict[str, Any]:
    """把一条文档增量加入本地索引（用户新建简历 / JD / 面经后调用）。

    调用方（`api_client`）**不应**让这里的失败影响主流程：任何异常都被收成
    `{"ok": False, "error": ...}` 返回，数据本身已落在本地主存，最坏情况只是
    「要等下次重建索引才能被检索到」。

    两条保护：
      · 索引尚未构建 → 直接返回 deferred，**不凭空造一个只有一条的索引**
        （`seed.ensure_seeded` 是「目标已存在就不铺」，先造会把随包种子永久挡住）；
      · 语义模型尚未加载 → 交后台线程，避免首次加载 ONNX（1~3s）卡住界面。
    """
    t = (text or "").strip()
    sid = str(source_id or "")
    if not source or not sid:
        return {"ok": False, "error": "缺少 source / source_id"}
    if len(t) < MIN_TEXT:
        return {"ok": False, "error": f"内容过短（< {MIN_TEXT} 字），未入索引"}

    store = _get_store()
    try:
        if not store.exists():
            return {"ok": False, "deferred": True,
                    "error": "本地索引尚未构建，留待下次重建时合并"}
        backend = store.get_meta("backend") or "semantic"
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    itype = item_type or {"resume": "resume", "jd": "jd", "article": "interview"}.get(source, "")
    if backend == "semantic" and not embedder.is_loaded():
        _spawn_background_index(source, sid, t, itype, created_at, backend)
        return {"ok": True, "deferred": "background", "source": source, "source_id": sid}

    try:
        return _write_document(source, sid, t, itype, created_at, backend)
    except Exception as exc:  # noqa: BLE001
        logger.warning("增量索引失败（%s:%s）：%s: %s", source, sid, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# 实体 → (取文本的键, 默认 source, item_type)，由 `_LOCAL_ENTITIES` 派生
_ENTITY_SPEC: Dict[str, Tuple[str, str, str]] = {
    entity: (text_key, source, item_type)
    for entity, source, item_type, text_key in _LOCAL_ENTITIES
}


def upsert_local(entity: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """把一条**本地主存记录**增量入索引（写入路径调用）。

    与 rebuild 的 `_local_records` 共用 `local_source_of` —— 「增量」和「重建」
    必须算出同一个 source，否则会出现「重建后成果在复用池、随手写一条又掉回
    材料池」这种只在一条路径上正确的错。
    """
    spec = _ENTITY_SPEC.get(str(entity or ""))
    if spec is None:
        return {"ok": False, "error": f"未知实体：{entity}"}
    text_key, default_source, item_type = spec
    if not isinstance(item, dict):
        return {"ok": False, "error": "记录不是 dict"}
    sid = str(item.get("id") or "")
    if not sid:
        return {"ok": False, "error": "记录缺少 id"}
    src = local_source_of(entity, item, default_source)
    return upsert_document(src, sid, item.get(text_key) or "", item_type=item_type,
                           created_at=item.get("created_at"))


def remove_document(source: str, source_id: str) -> Dict[str, Any]:
    """把一条文档从索引删掉（用户删除数据后调用）。索引不存在时无事可做。"""
    sid = str(source_id or "")
    if not source or not sid:
        return {"ok": False, "error": "缺少 source / source_id"}
    try:
        return {"ok": True, "removed": _get_store().delete_chunks(source, sid)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------- 检索（retrieve-then-generate 的检索端） ----------

def search(
    query: str,
    top_k: int = 5,
    item_type: Optional[str] = None,
    source: Optional[Any] = None,
    exclude_source: Optional[str] = None,
) -> Dict[str, Any]:
    """本地语义检索。索引不存在或模型不可用时给出明确提示，不静默返回空。

    `source` 可为单值或**列表**（白名单），透传给 VectorStore；`exclude_source`
    同理（生成简历时传 "resume" 以排除材料池）。两者都由调用方按场景选：
    「只要 corpus 范文」用 `source="corpus"`；「corpus + 自己的成果」用
    `source=["corpus", *WORK_SOURCES]`。
    返回体带 `backend`，**调用方必须据此换算阈值** —— 语义与 hash 词袋的
    余弦分数尺度差约 0.1~0.15，同一阈值不能两用（详见 retrieve.py）。
    """
    store = _get_store()
    st = store.stats()
    if not st.get("exists"):
        return {"ok": False, "error": "本地索引尚未构建，请先执行重建索引", "results": []}

    backend = store.get_meta("backend") or "semantic"
    qv = embedder.embed_text(query) if backend == "semantic" else embedder.embed_hash_fallback(query)
    if qv is None:
        return {"ok": False, "error": "本地 embedding 模型不可用", "results": []}

    results = store.search(qv, top_k=top_k, item_type=item_type, source=source,
                           exclude_source=exclude_source)
    return {"ok": True, "query": query, "count": len(results), "backend": backend, "results": results}


def context(query: str, top_k: int = 5, max_chars: int = 2400) -> str:
    """把检索结果拼成可喂给 LLM 的上下文文本（P3 的 retrieve-then-generate 用）。

    预留在此，保证「生成前先查库」只能走这一条路。
    """
    res = search(query, top_k=top_k)
    if not res.get("ok"):
        return ""
    buf: List[str] = []
    used = 0
    for r in res["results"]:
        block = f"[{r['item_type'] or r['source']} · 相似度 {r['score']}]\n{r['text']}"
        if used + len(block) > max_chars:
            break
        buf.append(block)
        used += len(block)
    return "\n\n".join(buf)


# ---------- 状态 ----------

def status() -> Dict[str, Any]:
    """索引 + 依赖 + 快照 + 本地增量的整体状态（不联网）。"""
    from . import pull as pull_mod

    store = _get_store()
    local_items: Dict[str, int] = {}
    try:
        from .. import local_store

        for entity, source, _itype, _key in _LOCAL_ENTITIES:
            local_items[source] = len(local_store.list_items(entity))
    except Exception:  # noqa: BLE001
        local_items = {}
    return {
        "embedder": embedder.backend_info(),
        "index": store.stats(),
        "sources": pull_mod.cache_status(),
        # 本地主存里「用户自己的数据」条数：这些会在重建时一并进索引
        "local_items": local_items,
        "index_db": INDEX_DB,
    }


__all__ = ["chunk_text", "rebuild", "search", "context", "status",
           "upsert_document", "upsert_local", "remove_document",
           "local_source_of", "WORK_SOURCES", "INDEX_DB"]
