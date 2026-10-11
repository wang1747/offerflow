"""事实核查节点：在简历优化后验证事实是否被篡改"""

from typing import TypedDict


class FactCheckerState(TypedDict):
    original_text: str
    optimized_text: str
    changes: list
    has_hallucination: bool


def check_facts(state: FactCheckerState) -> FactCheckerState:
    """检查优化后的文本是否引入了原始简历中没有的事实"""
    original = state.get("original_text", "")
    optimized = state.get("optimized_text", "")

    # TODO: 接入 LLM 进行语义级事实核查
    changes = []
    has_hallucination = False

    return {
        "original_text": original,
        "optimized_text": optimized,
        "changes": changes,
        "has_hallucination": has_hallucination,
    }
