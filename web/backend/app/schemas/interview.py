from pydantic import BaseModel
from typing import Optional, List


class ArticleImportRequest(BaseModel):
    company: str
    position: Optional[str] = None
    raw_content: str
    source: str = "manual"


class QuestionItem(BaseModel):
    id: str
    question: str
    answer: Optional[str] = None
    category: Optional[str] = None
    difficulty: Optional[str] = None


class SimulateAnswerRequest(BaseModel):
    session_id: str
    answer: str
