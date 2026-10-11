"""统一 embedding 入口：本地 fastembed 优先，失败降级到哈希词袋"""
import os
import hashlib
import math
import re
import threading
from pathlib import Path
from typing import List, Optional

# 国内访问 HuggingFace 不稳定，优先走镜像（仅本地模型缺失时兜底下载用）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_lock = threading.Lock()
_model = None
_DIM = 512  # bge-small-zh-v1.5 输出维度

# 本地模型目录：backend/models/fast-bge-small-zh-v1.5（随项目分发，无需联网下载）
_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "fast-bge-small-zh-v1.5"


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                _model = TextEmbedding(
                    "BAAI/bge-small-zh-v1.5",
                    specific_model_path=str(_MODEL_PATH),
                )
    return _model


def get_dim() -> int:
    return _DIM


def is_semantic_available() -> bool:
    try:
        _get_model()
        return True
    except Exception:
        return False


def embed_text(text: str) -> Optional[List[float]]:
    """真语义向量；模型不可用时返回 None（调用方降级）"""
    try:
        model = _get_model()
        # bge-small-zh 的 tokenizer 是 cased（大小写敏感），词表只含小写英文词，
        # 必须先 lower，否则 Python/FastAPI/Kubernetes 等大写技术词全被映射成 [UNK]
        lowered = (text or "").lower()
        vec = next(model.embed([lowered]))
        # fastembed 返回 numpy 数组，元素是 np.float32；ChromaDB upsert 只接受
        # Python 原生 float，必须显式转成原生 float 列表，否则报
        # "Expected embeddings to be a list of floats or ints" 导致语义匹配回退
        return [float(x) for x in vec]
    except Exception:
        return None


def embed_hash_fallback(text: str) -> List[float]:
    """原 MD5 词袋逻辑，保留为降级方案"""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    han = re.findall(r"[\u4e00-\u9fff]", text or "")
    tokens.extend(han)
    tokens.extend("".join(han[i : i + 2]) for i in range(len(han) - 1))
    vec = [0.0] * _DIM
    for token in tokens[:2000]:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "big") % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec
