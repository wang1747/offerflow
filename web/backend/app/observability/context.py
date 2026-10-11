"""可观测上下文：用 contextvars 在单次业务操作内传递 trace 信息"""

from __future__ import annotations

import contextvars
import time
import uuid
from typing import Any, Dict, List, Optional


class TraceContext:
    """一次业务操作对应的 trace 上下文（在 LLM callback 与落库之间共享）"""

    __slots__ = ("trace_id", "user_id", "operation", "spans", "_timers", "_models", "_trace_start")

    def __init__(self, trace_id: str, user_id: Optional[str], operation: str):
        self.trace_id = trace_id
        self.user_id = user_id
        self.operation = operation
        self.spans: List[Dict[str, Any]] = []
        self._timers: Dict[str, float] = {}
        self._models: Dict[str, str] = {}
        self._trace_start = time.time()

    def start_llm(self, run_id: str, model: Optional[str]):
        self._timers[run_id] = time.time()
        self._models[run_id] = model or ""

    def end_llm(
        self,
        run_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        status: str = "success",
    ):
        started = self._timers.pop(run_id, None)
        duration_ms = int((time.time() - started) * 1000) if started else 0
        start_offset_ms = int((started - self._trace_start) * 1000) if started else 0
        model = self._models.pop(run_id, "")
        self.spans.append(
            {
                "trace_id": self.trace_id,
                "run_id": str(run_id),
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "duration_ms": duration_ms,
                "start_offset_ms": start_offset_ms,
                "status": status,
            }
        )


_trace_var: contextvars.ContextVar[Optional[TraceContext]] = contextvars.ContextVar(
    "offerflow_trace", default=None
)


def get_current_trace() -> Optional[TraceContext]:
    return _trace_var.get()


def set_current_trace(ctx: TraceContext) -> contextvars.Token:
    return _trace_var.set(ctx)


def reset_current_trace(token: contextvars.Token) -> None:
    _trace_var.reset(token)


def new_trace_id() -> str:
    return str(uuid.uuid4())
