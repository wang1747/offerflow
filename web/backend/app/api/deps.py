from sqlalchemy.orm import Session

from app.core.database import get_db as _get_db


def get_db() -> Session:
    """数据库会话依赖注入"""
    yield from _get_db()
