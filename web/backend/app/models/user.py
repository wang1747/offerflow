import uuid

from sqlalchemy import Boolean, Column, DateTime, String, Text
from sqlalchemy.sql import func

from ..core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 昵称：仅用于界面展示，不作为登录标识
    name = Column(String, nullable=False)
    # 头像：存相对 URL（如 /api/v1/auth/avatar/xxx.png），空则前端显示首字母
    avatar = Column(String, nullable=True)
    # 账号唯一标识：邮箱。注册时即绑定，登录仅支持邮箱 + 密码
    email = Column(String, nullable=True, unique=True, index=True)
    # 手机号：模型层补充的映射，生产库已存在该列（autogenerate 基线已覆盖）
    phone = Column(String, nullable=True)
    hashed_password = Column(String, nullable=True)
    role = Column(String, nullable=False, default="user", server_default="user")
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    
    # LLM 配置（BYOK）- 严格按照计划字段
    llm_provider = Column(String, nullable=True)
    llm_base_url = Column(String, nullable=True)
    llm_model = Column(String, nullable=True)
    encrypted_api_key = Column(Text, nullable=True)

    # 是否同意把简历/JD/面经贡献到共享语料库。
    # 隐私默认关闭：用户必须在「设置」里显式打开才会贡献（注册流程不设置该字段，
    # 故这里的 Python default 即为新用户的真实初值，必须与 database.py 的
    # `ALTER ... DEFAULT false` 以及三处业务注释保持一致）。
    allow_corpus = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime, server_default=func.now())
