import uuid

from sqlalchemy import Boolean, Column, DateTime, JSON, String, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class CorpusItem(Base):
    """共享语料库条目（跨用户可检索的简历/JD/面经，喂给 RAG 检索）。

    与业务表（resumes / job_descriptions / interview_articles）解耦：
    用户同意「贡献语料」后才写入本表 + 整篇 embedding 存入 ChromaDB；
    匿名语料（爬虫/公开数据集导入）user_id 为空。
    """

    __tablename__ = "corpus_items"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    item_type = Column(String, nullable=False)          # resume | jd | interview
    raw_text = Column(Text, nullable=False)
    structured = Column(JSON, nullable=True)            # 解析后的结构化字段
    tags = Column(JSON, nullable=True)                  # 技能/关键词标签
    direction = Column(String, nullable=True)           # 求职方向标签（分类，可空）
    industry = Column(String, nullable=True)            # 行业标签（分类，可空）
    source = Column(String, nullable=True)              # user_upload | import | crawl
    source_id = Column(String, nullable=True)           # 关联业务表 id（resumes/job_descriptions/interview_articles），
                                                        # 用于删除业务数据时级联清理对应的语料副本；import/crawl 语料为空
    user_id = Column(String, ForeignKey("users.id"), nullable=True)  # 贡献者（空=匿名语料）
    is_public = Column(Boolean, nullable=False, default=False, server_default="0")
    embedding_id = Column(String, nullable=True)        # ChromaDB 中的向量 ID
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")
