from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EducationInput(BaseModel):
    school: str = ""
    major: str = ""
    degree: str = ""
    start: str = ""
    end: str = ""
    courses: str = ""   # 主修课程
    gpa: str = ""       # 绩点
    honors: str = ""    # 荣誉/获奖
    detail: str = ""    # 兼容旧数据，保留


class ExperienceInput(BaseModel):
    type: str = "project"  # project | internship | work
    name: str = ""
    role: str = ""
    start: str = ""
    end: str = ""
    bullets: List[str] = Field(default_factory=list)


class ResumeGenerateRequest(BaseModel):
    name: str
    position: str
    phone: Optional[str] = None
    email: Optional[str] = None
    github: Optional[str] = None
    summary: str = ""
    direction: Optional[str] = None  # 求职方向标签（如「技术研发」），用于匹配参考范文
    education: List[EducationInput] = Field(default_factory=list)
    experiences: List[ExperienceInput] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    jd_text: Optional[str] = None
    jd_id: Optional[str] = None


class RegenerateSectionRequest(BaseModel):
    resume_id: str
    structured: Dict[str, Any]
    section: str
    index: Optional[int] = None
    jd_text: Optional[str] = None
    jd_id: Optional[str] = None


class SaveStructuredRequest(BaseModel):
    resume_id: str
    structured: Dict[str, Any]
    jd_text: Optional[str] = None
    jd_id: Optional[str] = None
