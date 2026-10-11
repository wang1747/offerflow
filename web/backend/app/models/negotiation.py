"""薪资谈判会话模型。"""
import uuid

from sqlalchemy import Column, String, DateTime, Text, JSON, Integer, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class NegotiationSession(Base):
    __tablename__ = "negotiation_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)

    # 场景：offer(初次谈薪) / counter(应对压价) / raise(争取涨幅) / final(应对最终报价)
    scenario = Column(String, nullable=False, default="offer")
    # 用户前期设定：目标薪资、底线薪资、业绩证明点/背景
    target_salary = Column(String, nullable=True)
    bottom_salary = Column(String, nullable=True)
    context = Column(Text, nullable=True)

    status = Column(String, default="active")  # active | finished
    messages = Column(JSON, default=list)      # 对话历史 [{role: hr/user, content}]
    coaching = Column(JSON, default=list)      # 每轮教练反馈
    current_hr_message = Column(Text, nullable=True)  # 当前 HR 消息
    round_count = Column(Integer, default=0)   # 已进行的谈判轮数

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User")
