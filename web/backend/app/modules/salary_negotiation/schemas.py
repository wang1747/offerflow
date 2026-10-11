"""薪资谈判请求模型。"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class StartNegotiationRequest(BaseModel):
    scenario: str = Field("offer", description="场景：offer/counter/raise/final")
    target_salary: Optional[str] = Field(None, description="目标薪资")
    bottom_salary: Optional[str] = Field(None, description="底线薪资")
    context: str = Field("", description="业绩证明点/背景")


class SubmitAnswerRequest(BaseModel):
    answer: str = Field(..., min_length=1, description="回答内容")


class SalaryReferenceRequest(BaseModel):
    """薪资参考请求：根据简历估一个合理市场价。resume_id 与 resume_text 二选一。"""
    resume_id: Optional[str] = Field(None, description="已保存的简历 ID")
    resume_text: Optional[str] = Field(None, description="直接粘贴的简历文本")
    target_city: Optional[str] = Field(None, description="意向城市，如「北京」")
    position_hint: Optional[str] = Field(None, description="意向岗位，如「后端开发」")


class BenchmarkImportRequest(BaseModel):
    """导入权威市场薪资数据：覆盖模型估算，作为新的生效版本。"""
    data: Dict[str, Any] = Field(..., description="基准数据 {degree_base, direction_coef, city_tiers}")
    data_year: str = Field("", description="数据对应年份，如「2027」")
    source: str = Field("", description="数据来源说明，如「某机构 2027 届薪酬报告」")
    note: str = Field("", description="更新说明（可选）")
