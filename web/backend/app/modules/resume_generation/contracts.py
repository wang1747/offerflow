from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class GenerationResult:
    name: str = ""
    position: str = ""
    contact: Dict[str, str] = field(default_factory=dict)
    summary: str = ""
    education: List[Dict[str, Any]] = field(default_factory=list)
    experiences: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    tips: List[str] = field(default_factory=list)
    risks: List[Dict[str, Any]] = field(default_factory=list)
    jd_alignment: List[Dict[str, Any]] = field(default_factory=list)
    reference_source: str = ""
    resume_text: str = ""
    sections: List[str] = field(default_factory=list)
    ats: Dict[str, Any] = field(default_factory=dict)
    gap: Dict[str, Any] = field(default_factory=dict)
    fidelity: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)
    semantic: Dict[str, Any] = field(default_factory=dict)
    sufficiency: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
