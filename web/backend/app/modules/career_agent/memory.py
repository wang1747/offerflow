"""长期记忆服务：把**本轮运行的结构化结果**翻译成记忆记录。

## 为什么不让模型直接写记忆

设计文档 8.2 / 15.2 的规则是「模型推断的只能写成 ``proposed``，用户确认后才是
``confirmed``」。实现上有两种做法：

- ❌ 让模型在 compose 轮多输出一个 ``memories: [...]`` 字段 —— 模型自由文本
  直接变成记忆内容，注入一段 JD 文本就能往长期记忆里塞东西（记忆污染）。
- ✅ 由**服务端按模板**从结构化字段（decision / action / 业务版本）生成记忆内容。

本文件走后者：记忆内容是**确定性拼出来的**，模型能影响的只有它本来就负责的
``decision`` 与 ``summary``，而这两者都受长度上限与凭据特征过滤约束
（``memory_repository.assert_memory_content_allowed``），且一律先落 ``proposed``。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.models.agent_memory import AgentMemory
from app.modules.career_agent.memory_repository import (
    MAX_MEMORY_CONTENT_CHARS,
    MemoryContentRejected,
    MemoryRepository,
)

logger = logging.getLogger(__name__)

#: decision 枚举 → 给用户看的中文标签
DECISION_LABELS: Dict[str, str] = {
    "apply_now": "值得投递",
    "optimize_first": "先优化简历",
    "collect_evidence": "先补证据",
    "wait": "暂缓",
    "skip": "建议跳过",
}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


class MemoryService:
    """一批"记忆写入规则"。所有方法都先过内容约束，再交给仓储落库。"""

    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    def propose_decision(
        self,
        *,
        decision: Optional[str],
        summary: str,
        context_title: str,
        evidence_refs: Optional[List[str]] = None,
        context_version: Optional[str] = None,
    ) -> Optional[AgentMemory]:
        """把"本轮对某个岗位的判断"记成 ``proposed`` 决策记忆。

        - 状态恒为 ``proposed``：**用户确认前不是事实**。
        - ``conflict_key`` 用 ``decision:{context_version}``：同一个上下文版本下
          反复问，只保留最新判断（旧的标 superseded），不会攒出一串同类记忆。
        - 没有证据引用就不写：15.2 节要求记忆带 ``source_refs``，
          无来源的"模型感想"不该进长期记忆。
        """
        if not decision or decision not in DECISION_LABELS:
            return None
        refs = [r for r in (evidence_refs or []) if r]
        if not refs:
            return None

        label = DECISION_LABELS[decision]
        content = _clip(
            f"对「{context_title}」的判断：{label}。{_clip(summary, 120)}",
            MAX_MEMORY_CONTENT_CHARS,
        )
        try:
            return self._repo.create(
                content=content,
                memory_type="decision",
                source="system_event",
                status="proposed",
                source_refs=refs,
                confidence=0.6,
                importance=0.6,
                conflict_key=f"decision:{context_version or 'unknown'}",
            )
        except MemoryContentRejected as e:
            logger.info("决策记忆被内容约束拒绝：%s", e)
            return None

    # ------------------------------------------------------------------
    def record_outcome(
        self,
        *,
        action_type: str,
        target_label: str,
        detail: str,
        evidence_refs: Optional[List[str]] = None,
        context_version: Optional[str] = None,
    ) -> Optional[AgentMemory]:
        """写操作被用户确认并成功执行后，记一条 ``confirmed`` 结果记忆。

        这一条**可以是 confirmed**，理由不是"我们相信模型"，而是
        "这条记忆描述的是用户刚刚亲手确认并已发生的事实"（8.2 节：
        用户确认过的决策可以写入经历记忆）。
        """
        refs = [r for r in (evidence_refs or []) if r]
        if not refs:
            refs = [f"action:{context_version or 'unknown'}"]
        content = _clip(
            f"用户已确认执行「{action_type}」：{target_label} —— {detail}",
            MAX_MEMORY_CONTENT_CHARS,
        )
        try:
            return self._repo.create(
                content=content,
                memory_type="outcome",
                source="user_confirmed",
                status="confirmed",
                source_refs=refs,
                confidence=1.0,
                importance=0.8,
                conflict_key=f"outcome:{action_type}:{context_version or 'unknown'}",
            )
        except MemoryContentRejected as e:
            logger.info("结果记忆被内容约束拒绝：%s", e)
            return None

    # ------------------------------------------------------------------
    def add_user_preference(
        self,
        *,
        content: str,
        conflict_key: Optional[str] = None,
    ) -> AgentMemory:
        """用户**显式**添加的偏好（8.2 节：可标 ``user_explicit`` 直接确认）。

        ⚠️ 这条路径只能由 API 层在鉴权后调用，模型与 Graph 都不能走。
        """
        return self._repo.create(
            content=content,
            memory_type="preference",
            source="user_explicit",
            status="confirmed",
            source_refs=["user:explicit"],
            confidence=1.0,
            importance=0.9,
            conflict_key=conflict_key,
        )

    # ------------------------------------------------------------------
    def recalled_for_model(self, query: str, *, k: int = 5) -> List[Dict[str, Any]]:
        return self._repo.recall(query, k=k)
