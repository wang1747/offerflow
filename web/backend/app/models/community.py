"""社区分享：帖子 + 评论 + 点赞。"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class CommunityPost(Base):
    """分享墙帖子。category 取值：经验分享/面经/offer分享/资源推荐/其他。"""

    __tablename__ = "community_posts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String, nullable=False, default="经验分享")
    tags = Column(JSON, nullable=True)                 # 标签列表
    attachments = Column(JSON, nullable=True)          # 附件列表 [{name,kind,original_name,size,url}]
    like_count = Column(Integer, nullable=False, default=0, server_default="0")
    comment_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User")
    comments = relationship("CommunityComment", cascade="all, delete-orphan")
    likes = relationship("CommunityLike", cascade="all, delete-orphan")


class CommunityComment(Base):
    """帖子评论。"""

    __tablename__ = "community_comments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id = Column(String, ForeignKey("community_posts.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")
    post = relationship("CommunityPost", overlaps="comments")


class CommunityLike(Base):
    """帖子点赞（post_id + user_id 唯一，一人一赞）。"""

    __tablename__ = "community_likes"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_user"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id = Column(String, ForeignKey("community_posts.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())

    post = relationship("CommunityPost", overlaps="likes")
