"""薪资基准数据模型：把应届生起薪市场基准从「代码写死」升级为「可版本化落库」。

存三类数据（JSON）：学历底薪、岗位方向系数、城市档系数，
带 version / source / note / updated_at / is_active，支持定期刷新与回滚。
"""

import uuid

from sqlalchemy import Column, String, DateTime, Text, JSON, Integer, Boolean
from sqlalchemy.sql import func

from ..core.database import Base


class SalaryBenchmark(Base):
    __tablename__ = "salary_benchmarks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    version = Column(Integer, nullable=False, default=1)

    # 三类数据（JSON），结构与 core/salary_benchmark 的 DEFAULT_* 一致：
    #   degree_base:   {"associate": [4000, 6000], ...}
    #   direction_coef:{"ai": [1.70, "AI/算法/大模型"], ...}
    #   city_tiers:    {"tier1": [1.00, "一线城市（北上广深）"], ...}
    degree_base = Column(JSON, nullable=False)
    direction_coef = Column(JSON, nullable=False)
    city_tiers = Column(JSON, nullable=False)

    data_year = Column(String, nullable=True)   # 数据对应年份，如 "2026"
    source = Column(String, nullable=True)      # 数据来源说明
    note = Column(Text, nullable=True)          # 更新说明 / 调整依据

    is_active = Column(Boolean, default=True, nullable=False)  # 当前生效版本

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
