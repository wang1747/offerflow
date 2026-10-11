"""系统级设置：key-value 存储，供管理员后台读写

存放平台级配置（默认模型展示、用量配额、功能开关、公告等）。
value 统一存 JSON 字符串，便于扩展类型且兼容 SQLite/PG。
"""

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.sql import func

from ..core.database import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)          # JSON 字符串
    description = Column(String, nullable=True)   # 该项用途说明（供后台展示）
    updated_by = Column(String, nullable=True)    # 最后修改者 id
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# 设置项定义：(key, 默认值, 说明)
# 配额/开关的语义见 modules/admin/services.py 的 SETTING_DEFS
SETTING_DEFS = [
    ("quota_daily_tokens", 0, "每用户每日 token 配额（仅对使用平台默认 Key 的用户生效，0 = 不限制）"),
    ("allow_register", True, "是否开放新用户注册"),
    ("announcement", "", "平台公告内容（留空则不展示）"),
    ("announcement_enabled", False, "是否开启平台公告展示"),
]
