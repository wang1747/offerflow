"""管理员审计日志：记录管理员对系统/用户/内容的每一次写操作

设计要点：
- 冗余保存 operator_name / target_label：管理员改名或目标被删后，日志仍可读。
- detail 存 JSON 字符串：便于记录「旧值 -> 新值」，SQLite/PG 都兼容。
- 写入使用独立 session（见 modules/admin/audit.py），审计失败不阻断业务操作。
"""

import uuid

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.sql import func

from ..core.database import Base


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 操作者
    operator_id = Column(String, index=True, nullable=False)
    operator_name = Column(String, nullable=True)
    # 动作码：update_role / toggle_active / reset_password / bind_email /
    #          delete_post / delete_comment / delete_feedback / delete_corpus /
    #          update_setting / update_quota ...
    action = Column(String, index=True, nullable=False)
    # 目标对象
    target_type = Column(String, nullable=True)   # user / post / comment / feedback / corpus / setting
    target_id = Column(String, nullable=True)
    target_label = Column(String, nullable=True)  # 可读标识（邮箱 / 帖子标题 / 配置名）
    # 变更详情（JSON 字符串，含旧值/新值）
    detail = Column(Text, nullable=True)
    ip = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
