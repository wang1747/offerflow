from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date


class ApplicationCreate(BaseModel):
    company: str
    position: str
    jd_id: Optional[str] = None
    resume_id: Optional[str] = None


class ApplicationListItem(BaseModel):
    id: str
    company: str
    position: str
    status: str
    applied_date: Optional[date] = None
    next_action: Optional[str] = None
    next_action_date: Optional[date] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class EventCreate(BaseModel):
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    description: Optional[str] = None
