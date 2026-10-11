import uuid

from sqlalchemy import Column, String, Integer, Float, DateTime, ForeignKey
from sqlalchemy.sql import func

from ..core.database import Base


class Trace(Base):
    __tablename__ = "observability_traces"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True, nullable=True)
    operation = Column(String, nullable=False)
    status = Column(String, default="success")
    total_tokens = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)
    duration_ms = Column(Integer, default=0)
    span_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class Span(Base):
    __tablename__ = "observability_spans"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    trace_id = Column(String, ForeignKey("observability_traces.id"), index=True, nullable=False)
    run_id = Column(String, nullable=True)
    model = Column(String, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost = Column(Float, nullable=True)
    duration_ms = Column(Integer, default=0)
    start_offset_ms = Column(Integer, default=0)
    status = Column(String, default="success")
    created_at = Column(DateTime, server_default=func.now())
