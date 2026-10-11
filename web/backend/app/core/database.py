from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase

from ..config import get_settings

settings = get_settings()

# 创建数据库引擎
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ORM 基类（SQLAlchemy 2.0 标准）
class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    """依赖注入：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _register_all_models() -> None:
    """
    显式导入全部模型，确保 Base.metadata 完整。

    create_all 只会创建「已注册到 metadata」的表，若某模型没被 import 到，
    对应表就会被静默漏建（曾踩：新模型只被 admin 模块引用，测试脚本里
    在 init_db() 之后才 import，导致表没建出来）。
    """
    from app.models import (  # noqa: F401
        agent_action,
        agent_memory,
        agent_session,
        application,
        audit,
        community,
        corpus,
        feedback,
        interview,
        jd,
        match,
        negotiation,
        resume,
        salary_benchmark,
        setting,
        trace,
        user,
    )


def init_db():
    """初始化数据库，创建所有表"""
    _register_all_models()
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "users" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("users")}
        for column in (
            "hashed_password",
            "email",
            "llm_provider",
            "llm_base_url",
            "llm_model",
            "encrypted_api_key",
            "role",
            "is_active",
            "avatar",
        ):
            if column in columns:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} VARCHAR"))
        # allow_corpus 是布尔列，需用 BOOLEAN 补（VARCHAR 会让 '0' 被误读为 True）
        if "allow_corpus" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN allow_corpus BOOLEAN DEFAULT false"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET role = 'user' WHERE role IS NULL"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET is_active = true WHERE is_active IS NULL"))
    if "corpus_items" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("corpus_items")}
        # source_id：关联业务表 id，删除业务数据时级联清理语料副本
        if "source_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE corpus_items ADD COLUMN source_id VARCHAR"))
    if "community_posts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("community_posts")}
        # attachments：帖子附件元数据 [{name,kind,original_name,size,url}]
        if "attachments" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE community_posts ADD COLUMN attachments JSON"))
