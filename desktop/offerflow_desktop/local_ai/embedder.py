"""本地 embedding：fastembed + BAAI/bge-small-zh-v1.5（512 维，免联网）。

与服务器 `app/core/embeddings.py` **完全同构**：同一模型、同一 512 维、同一
`lower()` 预处理。同模型 + 同参数 → 同一向量空间，因此本地重算的向量与服务器
既有向量可比（P2 用本地重算替代"下载服务器向量"，正是靠这一点成立）。

为什么要 lower()：
    bge-small-zh 的 tokenizer 是 cased（大小写敏感），词表只含小写英文词。
    不 lower 的话 Python / FastAPI / Kubernetes 等大写技术词全被映射成 [UNK]，
    检索质量会明显下降（服务器端同样处理，见其注释）。

模型从哪来（按优先级）：
    1. 随包分发的 models/fast-bge-small-zh-v1.5（打包后位于 _MEIPASS 或 exe 同级）
    2. fastembed 默认缓存（首次会联网下载，作为兜底，非首选）

离线兜底：
    模型不可用（未分发 / 加载失败）时，`embed_hash_fallback()` 提供与服务器一致
    的哈希词袋向量，保证「检索功能不整体失效」，只是退化为词面匹配。
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import threading
from typing import List, Optional

# 国内访问 HuggingFace 不稳定，兜底下载走镜像（与服务器一致）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

DIM = 512
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_MODEL_DIRNAME = "fast-bge-small-zh-v1.5"

_lock = threading.Lock()
_model = None
_load_error: Optional[str] = None


def model_dir() -> Optional[str]:
    """返回随包分发的模型目录；不存在返回 None（调用方退回默认缓存）。"""
    candidates: List[str] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "models", _MODEL_DIRNAME))
        candidates.append(
            os.path.join(os.path.dirname(sys.executable), "models", _MODEL_DIRNAME)
        )
    try:
        from ..config import APP_ROOT

        candidates.append(os.path.join(APP_ROOT, "models", _MODEL_DIRNAME))
    except Exception:  # noqa: BLE001
        pass
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "model_optimized.onnx")):
            return c
    return None


def _get_model():
    global _model, _load_error
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                path = model_dir()
                try:
                    if path:
                        _model = TextEmbedding(MODEL_NAME, specific_model_path=path)
                    else:
                        _model = TextEmbedding(MODEL_NAME)
                    _load_error = None
                except Exception as exc:  # noqa: BLE001
                    _load_error = f"{type(exc).__name__}: {exc}"
                    raise
    return _model


def is_available() -> bool:
    """模型是否真的可用（加载成功才算，不是"装了包"就算）。"""
    try:
        _get_model()
        return True
    except Exception:  # noqa: BLE001
        return False


def is_loaded() -> bool:
    """模型是否**已在内存里**（不触发加载）。

    用途：写入路径（新建简历/JD/面经 → 增量进索引）不该因为「首次加载 ONNX 模型」
    把界面卡住 1~3 秒。调用方可据此决定「立刻增量索引」还是「留给下次重建」。
    索引的持久化不依赖它 —— 重建时会从本地主存重新组装（见 index.rebuild）。
    """
    return _model is not None


def load_error() -> str:
    return _load_error or ""


def get_dim() -> int:
    return DIM


def embed_texts(texts: List[str]) -> List[List[float]]:
    """批量生成语义向量（自动 lower）。失败抛异常，由调用方决定是否降级。"""
    model = _get_model()
    lowered = [(t or "").lower() for t in texts]
    return [[float(x) for x in v] for v in model.embed(lowered)]


def embed_text(text: str) -> Optional[List[float]]:
    """单条向量；模型不可用返回 None（与服务器同约定，调用方降级）。"""
    try:
        return embed_texts([text])[0]
    except Exception:  # noqa: BLE001
        return None


def embed_hash_fallback(text: str) -> List[float]:
    """MD5 词袋兜底（与服务器 `embed_hash_fallback` 同算法）。

    仅在模型不可用时使用：保证检索不整体失效，但只有词面匹配能力。
    """
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    han = re.findall(r"[\u4e00-\u9fff]", text or "")
    tokens.extend(han)
    tokens.extend("".join(han[i : i + 2]) for i in range(len(han) - 1))
    vec = [0.0] * DIM
    for token in tokens[:2000]:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "big") % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def backend_info() -> dict:
    """给 status() 用的自述信息。"""
    path = model_dir()
    return {
        "model": MODEL_NAME,
        "dim": DIM,
        "model_dir": path or "(fastembed 默认缓存，未随包分发)",
        "bundled": bool(path),
        "available": is_available(),
        "error": load_error(),
    }
