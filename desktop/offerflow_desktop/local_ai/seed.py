"""首启离线可用：把随包分发的「种子索引」铺到用户数据目录。

为什么需要（计划 §9.5-2）：
    索引的正常获取路径是「联网拉四路 → 本地重算」（首次约 20s）。**首启若无网络，
    本地就完全没有检索能力**，与「离线可用」的目标冲突。
    而共享语料是全站同一份（`corpus`），完全可以随安装包带一份算好的
    `index.db` + `corpus` 快照，让用户**开箱即有离线检索**。

种子里装什么：
    **只装共享语料（corpus）**，不装任何用户私有数据（简历 / JD）—— 那些由用户
    登录后自己拉。种子的作用只是「让检索从第一秒就能用」，登录后一次正常重建
    就会把个人数据补进来。

怎么校验种子可用：
    种子 `index.db` 的 meta 里记了 `backend` 与 `model`。**只有与当前环境一致时才铺** ——
    换了模型 / 换分词后向量空间不同，用种子的向量去比查询向量毫无意义
    （会得到几乎随机的分数）。模型名不一致时直接跳过，让用户走正常重建。

幂等：目标已存在 `index.db` 时**不动**（用户自己的数据永远优先）。
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SEED_DIRNAME = os.path.join("seed", "local_ai")


def seed_root() -> Optional[str]:
    """随包种子目录（内含 index.db 与 cache/）；找不到返回 None。"""
    cands: List[str] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            cands.append(os.path.join(meipass, SEED_DIRNAME))
        cands.append(os.path.join(os.path.dirname(sys.executable), SEED_DIRNAME))
    try:
        from ..config import APP_ROOT

        cands.append(os.path.join(APP_ROOT, SEED_DIRNAME))
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        if os.path.exists(os.path.join(c, "index.db")):
            return c
    return None


def _seed_meta(db_path: str) -> Dict[str, str]:
    """只读打开种子 db，取 meta 表（**不要**用 VectorStore，避免在种子目录里建表）。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT key, value FROM meta").fetchall()
        finally:
            con.close()
        return {str(k): ("" if v is None else str(v)) for k, v in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取种子 meta 失败：%s: %s", type(exc).__name__, exc)
        return {}


def ensure_seeded(force: bool = False) -> Dict[str, Any]:
    """首次运行时铺设种子索引（幂等）。

    force=True 时即使目标已存在也覆盖（仅供测试 / 「恢复初始语料」类操作）。
    """
    from . import embedder
    from .paths import CACHE_DIR, LOCAL_AI_DIR, ensure_dirs

    target_dir = LOCAL_AI_DIR
    target_db = os.path.join(target_dir, "index.db")

    if os.path.exists(target_db) and not force:
        return {"seeded": False, "reason": "already_exists", "target": target_db}

    root = seed_root()
    if not root:
        return {"seeded": False, "reason": "no_seed", "target": target_db}

    src_db = os.path.join(root, "index.db")
    if os.path.abspath(src_db) == os.path.abspath(target_db):
        return {"seeded": False, "reason": "same_path", "target": target_db}

    meta = _seed_meta(src_db)
    if not meta.get("backend"):
        return {"seeded": False, "reason": "seed_invalid", "detail": "种子缺少 backend 元数据",
                "target": target_db}

    seed_model = meta.get("model") or ""
    current_model = embedder.MODEL_NAME
    if seed_model and seed_model != current_model:
        logger.warning("种子索引模型不匹配（seed=%s / current=%s），跳过铺设",
                       seed_model, current_model)
        return {"seeded": False, "reason": "model_mismatch",
                "seed_model": seed_model, "current_model": current_model,
                "target": target_db}
    if not seed_model:
        logger.warning("种子索引未记录 model（旧版种子），按可用处理但不做模型校验")

    ensure_dirs()
    os.makedirs(target_dir, exist_ok=True)
    shutil.copy2(src_db, target_db)
    copied = ["index.db"]

    # 快照一起铺：离线重建（rebuild(do_pull=False)）需要它
    src_cache = os.path.join(root, "cache")
    if os.path.isdir(src_cache):
        os.makedirs(CACHE_DIR, exist_ok=True)
        for fn in sorted(os.listdir(src_cache)):
            if not fn.endswith(".json"):
                continue
            dst = os.path.join(CACHE_DIR, fn)
            if os.path.exists(dst):
                continue  # 不覆盖用户已有快照
            shutil.copy2(os.path.join(src_cache, fn), dst)
            copied.append(f"cache/{fn}")

    logger.info("已铺设种子索引：%s（%s）", target_db, ", ".join(copied))
    return {
        "seeded": True,
        "reason": "ok",
        "target": target_db,
        "files": copied,
        "backend": meta.get("backend"),
        "model": seed_model or current_model,
        "built_at": meta.get("built_at"),
        "seed_root": root,
    }


__all__ = ["SEED_DIRNAME", "seed_root", "ensure_seeded"]
