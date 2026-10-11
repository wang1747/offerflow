import uuid

from sqlalchemy import Column, String, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class JobDescription(Base):
    __tablename__ = "job_descriptions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    raw_text = Column(Text, nullable=False)
    company = Column(String, nullable=True)
    position = Column(String, nullable=True)
    must_have = Column(JSON, nullable=True)
    nice_to_have = Column(JSON, nullable=True)
    tech_stack = Column(JSON, nullable=True)
    hidden_signals = Column(JSON, nullable=True)
    embedding_id = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    # 关联用户模型，方便联查
    user = relationship("User")
