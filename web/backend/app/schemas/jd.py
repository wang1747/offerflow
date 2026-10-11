from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class JDParseRequest(BaseModel):
    raw_text: str


class JDParseResponse(BaseModel):
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None


class JDListItem(BaseModel):
    id: str
    company: Optional[str] = None
    position: Optional[str] = None
    must_have: Optional[List[str]] = None
    nice_to_have: Optional[List[str]] = None
    tech_stack: Optional[Dict[str, List[str]]] = None
    hidden_signals: Optional[List[str]] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
