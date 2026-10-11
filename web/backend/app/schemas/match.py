"""
匹配模块 Pydantic 模型
"""

from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field


class MatchRequest(BaseModel):
    jd_id: str = Field(..., min_length=1, description="JD ID", examples=["jd_123"])
    resume_id: str = Field(..., min_length=1, description="简历 ID", examples=["resume_456"])


class MatchResultDetail(BaseModel):
    matched: List[str] = Field(default_factory=list, description="匹配的技能")
    missing: List[str] = Field(default_factory=list, description="缺失的技能")
    partial: List[str] = Field(default_factory=list, description="部分匹配的技能")


class MatchGapAnalysis(BaseModel):
    hard_gap: List[str] = Field(default_factory=list, description="硬性差距")
    soft_gap: List[str] = Field(default_factory=list, description="软性差距")
    suggestion: str = Field(default="", description="建议")


class MatchResultItem(BaseModel):
    jd_id: str
    resume_id: str
    score: float
    skill_match_detail: Optional[Dict] = None
    gap_analysis: Optional[Dict] = None
    suggestion: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class MatchResponse(BaseModel):
    id: str
    jd_id: str
    resume_id: str
    company: Optional[str] = None
    position: Optional[str] = None
    score: int
    skill_match_detail: MatchResultDetail
    gap_analysis: MatchGapAnalysis
    suggestion: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RankingItem(BaseModel):
    id: str
    jd_id: str
    resume_id: str
    company: Optional[str] = None
    position: Optional[str] = None
    score: float
    suggestion: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}