"""简历优化子模块对外接口。"""

from .contracts import (
    Change,
    GapAnalysis,
    JDRequirement,
    OptimizationResult,
    PreservationResult,
    ResumeSection,
)
from .gap import analyze_gap
from .service import optimize_resume_text

__all__ = [
    "optimize_resume_text",
    "analyze_gap",
    "OptimizationResult",
    "PreservationResult",
    "ResumeSection",
    "Change",
    "GapAnalysis",
    "JDRequirement",
]
