"""项目建议反馈：用户提建议，公开 + 状态跟踪 + 管理员回复。"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class Feedback(Base):
    """建议反馈。category：功能建议/问题反馈/体验优化/其他；status：pending/accepted/declined/replied。"""

    __tablename__ = "feedbacks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    category = Column(String, nullable=False, default="功能建议")
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="pending", server_default="pending")
    admin_reply = Column(Text, nullable=True)
    like_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User")
    likes = relationship("FeedbackLike", cascade="all, delete-orphan")


class FeedbackLike(Base):
    """建议点赞（feedback_id + user_id 唯一）。"""

    __tablename__ = "feedback_likes"
    __table_args__ = (UniqueConstraint("feedback_id", "user_id", name="uq_feedback_user"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    feedback_id = Column(String, ForeignKey("feedbacks.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())

    feedback = relationship("Feedback", overlaps="likes")
