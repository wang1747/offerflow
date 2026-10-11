"""LLM 观测器：LangChain callback，无侵入录制每次 LLM 调用的 token/耗时/成本"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.observability.context import get_current_trace

logger = logging.getLogger(__name__)


def _extract_model(serialized: dict[str, Any]) -> Optional[str]:
    """从 serialized 信息中提取模型名"""
    if not isinstance(serialized, dict):
        return None
    kwargs = serialized.get("kwargs") or {}
    if isinstance(kwargs, dict):
        model = kwargs.get("model") or kwargs.get("model_name")
        if model:
            return str(model)
    return serialized.get("name")


def _extract_tokens(response: LLMResult) -> tuple[int, int]:
    """从 LLMResult 中提取 (prompt_tokens, completion_tokens)"""
    prompt = 0
    completion = 0

    # 方式一：AIMessage.usage_metadata（langchain 1.x 标准字段）
    try:
        generation = response.generations[0][0]
        message = getattr(generation, "message", None)
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    except Exception:  # noqa: BLE001
        pass

    # 方式二：llm_output 里的 token_usage / usage
    if not prompt and not completion:
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            if isinstance(usage, dict):
                prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    return prompt, completion


class LLMObserver(BaseCallbackHandler):
    """录制 LLM 调用到当前 trace 上下文（通过 contextvar 关联）"""

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        ctx = get_current_trace()
        if ctx is None:
            return
        ctx.start_llm(str(run_id), _extract_model(serialized))

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        ctx = get_current_trace()
        if ctx is None:
            return
        prompt, completion = _extract_tokens(response)
        ctx.end_llm(str(run_id), prompt, completion, status="success")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        ctx = get_current_trace()
        if ctx is None:
            return
        ctx.end_llm(str(run_id), 0, 0, status="error")


_observer: Optional[LLMObserver] = None


def get_observer() -> LLMObserver:
    """返回全局单例 observer"""
    global _observer
    if _observer is None:
        _observer = LLMObserver()
    return _observer
