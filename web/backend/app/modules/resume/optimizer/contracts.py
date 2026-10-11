"""简历优化模块的对外数据结构。

这些类型只负责在优化模块内部和调用方之间传递信息，不依赖数据库或 HTTP 层。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class JDRequirement:
    """目标 JD 的结构化需求，用于驱动差距分析与关键词对齐。"""

    raw_text: str = ""
    company: str = ""
    position: str = ""
    must_have: List[str] = field(default_factory=list)
    nice_to_have: List[str] = field(default_factory=list)
    tech_stack: List[str] = field(default_factory=list)
    hidden_signals: List[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        """把结构化需求拼成给 LLM 看的精简文本。"""
        parts: List[str] = []
        if self.position:
            parts.append(f"目标职位：{self.position}")
        if self.company:
            parts.append(f"目标公司：{self.company}")
        if self.must_have:
            parts.append("必备技能/要求：" + "、".join(self.must_have))
        if self.nice_to_have:
            parts.append("加分项：" + "、".join(self.nice_to_have))
        if self.tech_stack:
            parts.append("技术栈：" + "、".join(self.tech_stack))
        if self.hidden_signals:
            parts.append("软性信号：" + "、".join(self.hidden_signals))
        if self.raw_text:
            parts.append("\nJD 原文：\n" + self.raw_text)
        return "\n".join(parts)


@dataclass
class GapAnalysis:
    """简历相对 JD 的差距分析结果。"""

    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    partial: List[str] = field(default_factory=list)


@dataclass
class Change:
    """一处具体改动，用于前端 diff 展示。"""

    section: str = ""
    before: str = ""
    after: str = ""
    reason: str = ""


@dataclass
class PreservationResult:
    score: float
    passed: bool
    missing_facts: List[str] = field(default_factory=list)
    added_facts: List[str] = field(default_factory=list)
    fallback: bool = False
    # 硬事实分类计数（邮箱/电话/链接/时间），用于前端「事实保真」可视化
    critical_facts: Dict[str, int] = field(default_factory=dict)


@dataclass
class ResumeSection:
    index: int
    original: str
    rewritten: str
    changed: bool = False


@dataclass
class OptimizationResult:
    optimized_text: str
    changes: List[Change] = field(default_factory=list)
    added_keywords: List[str] = field(default_factory=list)
    removed_keywords: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)  # 删除/精简的内容说明
    length_warning: str = ""  # 非空表示结果可能仍超一页，需提示用户
    gap: GapAnalysis = field(default_factory=GapAnalysis)
    sections: List[ResumeSection] = field(default_factory=list)
    edit_count: int = 0
    preservation: PreservationResult = field(
        default_factory=lambda: PreservationResult(score=1.0, passed=True)
    )
    error: str = ""
