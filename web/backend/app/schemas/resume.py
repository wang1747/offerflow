from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class ResumeListItem(BaseModel):
    id: str
    version: int
    source_file: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OptimizeRequest(BaseModel):
    resume_id: str
    jd_id: str


class OptimizeResponse(BaseModel):
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
