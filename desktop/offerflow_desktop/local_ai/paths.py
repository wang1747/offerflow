"""本地 AI 数据目录统一入口。

桌面版「本地离线 AI」的全部落盘数据集中在这里，便于：
  · 打包时一眼分清哪些随包、哪些运行时生成；
  · 备份 / 清理时边界清晰；
  · P2/P3 各模块复用同一套路径，不散落硬编码。

目录布局（根 = config.DATA_DIR/local_ai）：

    local_ai/
        index.db      轻量向量库：单文件 SQLite（元数据 + 向量 BLOB）（P2）
        knowledge.db  本地图谱与记忆：实体/关系/文档映射（P3b）+ 记忆向量（P3c）
        cache/        四路数据拉取快照 JSON（P2：corpus / resumes / jd / salary）
        config.json   本地 AI 配置（模型名、分块参数等）

为什么图谱/记忆要单独一个库（而不是塞进 index.db）：
    `rebuild()` 会 `store.clear()` 清表重建。图谱与记忆的代价是**真金白银的
    模型调用换来的**，绝不能因为「重建一次索引」就被清掉。分库 + 不同表名，
    两边彻底隔离（VectorStore 支持 table 参数，见 store.py）。

为什么不是 chroma/：
    P2 实测 chromadb 全依赖 76MB / 61 个包（opentelemetry 全家套件 + kubernetes 客户端
    + uvicorn + 遥测上报），而本机语料只有 362 条目 / 518 块，暴力余弦检索仅 1.2ms。
    故改用自研 VectorStore（store.py）：SQLite 单文件 + numpy 点积，零额外依赖、纯离线。
    P0 时期建的 chroma/ 空目录已废弃（不再创建）。

为什么挂在 DATA_DIR 下：
    打包安装版时 DATA_DIR = %LOCALAPPDATA%\\OfferFlow\\data，升级 / 卸载不丢用户数据；
    源码运行时 DATA_DIR = <项目根>/data，便于开发调试。
    见 config._data_dir()。

注意：业务数据不在本目录，沿用 data/offerflow.db（P4 升级为「本地主存 + 定时同步」）。
"""
from __future__ import annotations

import os
from typing import List

from ..config import DATA_DIR

# ---- 本地 AI 目录树 ----
LOCAL_AI_DIR = os.path.join(DATA_DIR, "local_ai")
CACHE_DIR = os.path.join(LOCAL_AI_DIR, "cache")
CONFIG_PATH = os.path.join(LOCAL_AI_DIR, "config.json")

# 轻量向量库：单文件 SQLite（P2）。不用目录形态（Chroma）——理由见模块头注释。
INDEX_DB_PATH = os.path.join(LOCAL_AI_DIR, "index.db")

# 本地图谱 + 记忆（P3b/P3c）。**必须与 index.db 分开**：索引重建会清表，
# 而图谱/记忆是模型调用换来的，不能被一次重建带走。
KNOWLEDGE_DB_PATH = os.path.join(LOCAL_AI_DIR, "knowledge.db")

# 需要确保存在的目录（config.json / index.db / knowledge.db 是文件，不在此列）
_ALL_DIRS = (LOCAL_AI_DIR, CACHE_DIR)


def ensure_dirs() -> List[str]:
    """创建本地 AI 全部目录（幂等），返回已确保存在的目录列表。

    在 P0 初始化时调用一次即可；后续各阶段写入前也可重复调用（无害）。
    """
    for d in _ALL_DIRS:
        os.makedirs(d, exist_ok=True)
    return list(_ALL_DIRS)


def cache_path(name: str) -> str:
    """四路拉取快照的文件路径，例如 cache_path("corpus") -> .../cache/corpus.json。"""
    return os.path.join(CACHE_DIR, f"{name}.json")
