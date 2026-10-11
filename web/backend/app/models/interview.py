import uuid

from sqlalchemy import Column, String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class InterviewArticle(Base):
    __tablename__ = "interview_articles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    company = Column(String, nullable=True)
    position = Column(String, nullable=True)
    raw_content = Column(Text, nullable=True)
    questions = Column(JSON, nullable=True)
    tags = Column(JSON, nullable=True)
    difficulty = Column(String, nullable=True)
    embedding_id = Column(String, nullable=True)
    source = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")
    question_list = relationship("InterviewQuestion")


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    article_id = Column(String, ForeignKey("interview_articles.id"), nullable=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    category = Column(String, nullable=True)
    difficulty = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    article = relationship("InterviewArticle", overlaps="question_list")
    user = relationship("User")

class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    resume_id = Column(String, nullable=False)
    jd_id = Column(String, nullable=False)
    status = Column(String, default="active")  # active | finished
    questions = Column(JSON, default=list)     # 历史问题列表
    answers = Column(JSON, default=list)       # 历史回答列表
    feedbacks = Column(JSON, default=list)     # 历史反馈列表
    current_question = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User")
