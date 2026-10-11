"""桌面版「本地离线 AI」模块（自 P0 起建）。

设计原则见 offerflow_desktop_plan.md：
  · AI 重算全在本地（解析 / embedding / 检索 / 记忆），离线可用；
  · 业务数据本地优先，服务器只做被动真源 + 鉴权；
  · 大模型每次生成前强制先检索本地索引（retrieve-then-generate）。

当前子模块：
    paths     本地 AI 数据目录统一入口（P0）
    parse     本地文档解析：PDF(LiteParse→OCR→pypdf) / DOCX / TXT / 图片 OCR（P1）
    pull      四路全量拉取 → cache/*.json 快照（P2）
    embedder  fastembed + bge-small-zh-v1.5（512 维，本地模型，免联网）（P2）
    store     轻量向量库：SQLite 单文件 + numpy 余弦检索（P2，替代 Chroma）
    index     编排：分块 → embedding → 入库 → 检索（P2）
    retrieve  **统一检索入口**：所有生成前查库都走它（P3，retrieve-then-generate）
    graph     本地实体-关系图谱：多跳检索补充（P3b）
    memory    本地记忆：关于用户的稳定事实（P3c）
"""
from . import embedder, freshness, graph, index, memory, pull, retrieve, seed, store
from .freshness import check as check_index_freshness
from .freshness import local_state as local_index_state
from .index import rebuild as rebuild_index, search as search_index, status as local_ai_status
from .parse import (
    SUPPORTED_EXT,
    ParseResult,
    extract_text,
    parse_document,
    parse_status,
)
from .paths import (
    CACHE_DIR,
    CONFIG_PATH,
    INDEX_DB_PATH,
    KNOWLEDGE_DB_PATH,
    LOCAL_AI_DIR,
    cache_path,
    ensure_dirs,
)
from .pull import PullError, cache_status, load_cache, pull_all, pull_one
from .retrieve import retrieve_context, retrieve_text
from .seed import ensure_seeded
from .store import VectorStore

__all__ = [
    # paths
    "LOCAL_AI_DIR",
    "CACHE_DIR",
    "CONFIG_PATH",
    "INDEX_DB_PATH",
    "KNOWLEDGE_DB_PATH",
    "ensure_dirs",
    "cache_path",
    # parse
    "ParseResult",
    "parse_document",
    "parse_status",
    "extract_text",
    "SUPPORTED_EXT",
    # pull
    "pull_all",
    "pull_one",
    "load_cache",
    "cache_status",
    "PullError",
    # retrieve（P3 统一检索入口）
    "retrieve",
    "retrieve_context",
    "retrieve_text",
    # embedder / store / index
    "embedder",
    "store",
    "index",
    "pull",
    "VectorStore",
    "rebuild_index",
    "search_index",
    "local_ai_status",
    # graph / memory（P3b / P3c）
    "graph",
    "memory",
]
