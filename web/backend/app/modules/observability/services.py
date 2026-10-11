"""可观测查询服务：trace 列表 / 详情 / 成本汇总"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.trace import Span, Trace
from app.observability.cost import usd_to_cny


def _trace_dict(t: Trace) -> dict:
    return {
        "id": t.id,
        "operation": t.operation,
        "status": t.status,
        "total_tokens": t.total_tokens,
        "total_cost": t.total_cost,
        "total_cost_cny": usd_to_cny(t.total_cost),
        "duration_ms": t.duration_ms,
        "span_count": t.span_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def list_traces(
    db: Session,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    operation: Optional[str] = None,
) -> List[dict]:
    query = db.query(Trace).filter(Trace.user_id == user_id)
    if operation:
        query = query.filter(Trace.operation == operation)
    rows = (
        query.order_by(Trace.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return [_trace_dict(t) for t in rows]


def get_trace_detail(db: Session, user_id: str, trace_id: str) -> Optional[dict]:
    trace = db.query(Trace).filter(
        Trace.id == trace_id,
        Trace.user_id == user_id,
    ).first()
    if not trace:
        return None

    spans = (
        db.query(Span)
        .filter(Span.trace_id == trace_id)
        .order_by(Span.created_at.asc())
        .all()
    )
    return {
        "trace": _trace_dict(trace),
        "spans": [
            {
                "id": s.id,
                "run_id": s.run_id,
                "model": s.model,
                "prompt_tokens": s.prompt_tokens,
                "completion_tokens": s.completion_tokens,
                "total_tokens": s.total_tokens,
                "cost": s.cost,
                "cost_cny": usd_to_cny(s.cost),
                "duration_ms": s.duration_ms,
                "start_offset_ms": s.start_offset_ms,
                "status": s.status,
            }
            for s in spans
        ],
    }


def get_summary(db: Session, user_id: str) -> dict:
    """成本汇总：总费用/token/trace 数，按 operation 与模型聚合"""
    traces = db.query(Trace).filter(Trace.user_id == user_id).all()

    total_cost = round(sum(t.total_cost or 0 for t in traces), 6)
    total_tokens = sum(t.total_tokens or 0 for t in traces)
    trace_count = len(traces)

    by_operation_map: dict[str, dict] = {}
    for t in traces:
        item = by_operation_map.setdefault(
            t.operation or "未知", {"operation": t.operation or "未知", "cost": 0.0, "tokens": 0, "count": 0}
        )
        item["cost"] += t.total_cost or 0
        item["tokens"] += t.total_tokens or 0
        item["count"] += 1
    by_operation = sorted(
        (
            {
                "operation": v["operation"],
                "cost": round(v["cost"], 6),
                "cost_cny": usd_to_cny(round(v["cost"], 6)),
                "tokens": v["tokens"],
                "count": v["count"],
            }
            for v in by_operation_map.values()
        ),
        key=lambda x: x["cost"],
        reverse=True,
    )

    # 按模型聚合（从 spans join traces 过滤用户）
    by_model_rows = (
        db.query(Span.model, func.sum(Span.cost), func.sum(Span.total_tokens), func.count(Span.id))
        .join(Trace, Trace.id == Span.trace_id)
        .filter(Trace.user_id == user_id)
        .group_by(Span.model)
        .all()
    )
    by_model = sorted(
        (
            {
                "model": model or "未知",
                "cost": round(cost or 0, 6),
                "cost_cny": usd_to_cny(round(cost or 0, 6)),
                "tokens": int(tokens or 0),
                "count": int(count or 0),
            }
            for model, cost, tokens, count in by_model_rows
        ),
        key=lambda x: x["cost"],
        reverse=True,
    )

    return {
        "total_cost": total_cost,
        "total_cost_cny": usd_to_cny(total_cost),
        "total_tokens": total_tokens,
        "trace_count": trace_count,
        "by_operation": by_operation,
        "by_model": by_model,
    }
