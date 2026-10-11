from typing import List, Optional

from pydantic import BaseModel


class CorpusAddRequest(BaseModel):
    item_type: str  # resume | jd | interview
    raw_text: str
    structured: Optional[dict] = None
    tags: Optional[List[str]] = None
    is_public: bool = False


class CorpusSearchRequest(BaseModel):
    query_text: str
    top_k: int = 5
    item_type: Optional[str] = None
    direction: Optional[str] = None
