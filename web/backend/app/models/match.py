import uuid

from sqlalchemy import Column, String, DateTime, Text, Float, JSON, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class MatchResult(Base):
    __tablename__ = "match_results"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    jd_id = Column(String, ForeignKey("job_descriptions.id"), nullable=False)
    resume_id = Column(String, ForeignKey("resumes.id"), nullable=False)
    score = Column(Float, nullable=False)
    skill_match_detail = Column(JSON, nullable=True)
    gap_analysis = Column(JSON, nullable=True)
    suggestion = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    # 关联JD与简历模型
    jd = relationship("JobDescription")
    resume = relationship("Resume")
