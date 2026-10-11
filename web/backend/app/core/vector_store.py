import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import get_settings


def get_vector_store():
    """获取 ChromaDB 客户端（持久化模式）"""
    settings = get_settings()
    client = chromadb.PersistentClient(
        path=settings.chroma_persist_path,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    return client


def get_or_create_collection(client, name: str = "jd_embeddings", metadata=None):
    """获取或创建集合"""
    return client.get_or_create_collection(name=name, metadata=metadata)
