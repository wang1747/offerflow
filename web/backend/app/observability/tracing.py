"""trace 生命周期管理：trace_operation 上下文 + 落库 flush"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from app.core.database import SessionLocal
from app.models.trace import Span, Trace
from app.observability.context import (
    TraceContext,
    new_trace_id,
    reset_current_trace,
    set_current_trace,
)
from app.observability.cost import calculate_cost

logger = logging.getLogger(__name__)


def _flush(ctx: TraceContext, duration_ms: int) -> None:
    """把一次操作累积的 spans 落库为 Trace + Span（独立 session，线程安全）"""
    if not ctx.spans:
        return

    total_tokens = 0
    total_cost = 0.0
    status = "success"
    span_models = []

    for s in ctx.spans:
        total_tokens += s["total_tokens"]
        cost = calculate_cost(s["model"], s["prompt_tokens"], s["completion_tokens"])
        if cost is not None:
            total_cost += cost
        if s["status"] == "error":
            status = "failed"
        span_models.append(
            Span(
                trace_id=ctx.trace_id,
                run_id=s["run_id"],
                model=s["model"] or None,
                prompt_tokens=s["prompt_tokens"],
                completion_tokens=s["completion_tokens"],
                total_tokens=s["total_tokens"],
                cost=cost,
                duration_ms=s["duration_ms"],
                start_offset_ms=s.get("start_offset_ms", 0),
                status=s["status"],
            )
        )

    db = SessionLocal()
    try:
        trace = Trace(
            id=ctx.trace_id,
            user_id=ctx.user_id,
            operation=ctx.operation,
            status=status,
            total_tokens=total_tokens,
            total_cost=round(total_cost, 6),
            duration_ms=duration_ms,
            span_count=len(span_models),
        )
        db.add(trace)
        # 必须先 flush 把 trace 落库再写 spans：
        # Span.trace_id 只有外键、没有 ORM relationship，SQLAlchemy 无法推断插入顺序，
        # 会先插 spans 导致外键违反（observability_spans_trace_id_fkey），整笔回滚、数据全丢。
        db.flush()
        db.add_all(span_models)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("可观测数据落库失败")
    finally:
        db.close()


@contextmanager
def trace_operation(operation: str, user_id: Optional[str]) -> Iterator[TraceContext]:
    """包住一次业务操作，自动创建 trace 并落库"""
    ctx = TraceContext(trace_id=new_trace_id(), user_id=user_id, operation=operation)
    token = set_current_trace(ctx)
    started = time.time()
    try:
        yield ctx
    finally:
        reset_current_trace(token)
        _flush(ctx, int((time.time() - started) * 1000))
