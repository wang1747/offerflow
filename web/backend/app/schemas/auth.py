"""
认证模块 Pydantic 模型
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class UserResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    email: Optional[str] = None
    role: Optional[str] = None
    avatar: Optional[str] = None

    model_config = {"from_attributes": True}


class RegisterRequest(BaseModel):
    account: str = Field(
        ...,
        min_length=3,
        max_length=64,
        description="账号：邮箱（唯一登录标识）",
        examples=["zhangsan@example.com"]
    )
    name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=32,
        description="用户称呼/昵称（可选，留空则按账号自动生成）",
        examples=["张三"]
    )
    password: str = Field(
        ...,
        min_length=6,
        description="密码最少6位，建议同时包含大小写字母、数字",
        examples=["Pass123456"]
    )


class RegisterResponse(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str = Field(..., description="登录邮箱", examples=["zhangsan@example.com"])
    password: str = Field(..., description="密码", examples=["Pass123456"])


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

    model_config = {"from_attributes": True}


class SendCodeRequest(BaseModel):
    account: str = Field(..., description="账号：注册邮箱")
    email: Optional[str] = Field(None, description="接收验证码的邮箱（留空则发往账号邮箱）")


class ResetPasswordRequest(BaseModel):
    account: str = Field(..., description="账号：注册邮箱")
    email: str = Field(..., description="接收验证码的邮箱")
    code: str = Field(..., description="6 位验证码")
    new_password: str = Field(..., min_length=6, description="新密码（至少 6 位）")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="当前密码")
    new_password: str = Field(..., min_length=6, description="新密码（至少 6 位）")


class ProfileUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=32, description="用户昵称")


class ChangeEmailSendCodeRequest(BaseModel):
    new_email: str = Field(..., description="待绑定的新邮箱，验证码将发送到该邮箱")


class ChangeEmailRequest(BaseModel):
    new_email: str = Field(..., description="新邮箱")
    code: str = Field(..., description="发送到新邮箱的 6 位验证码")


__all__ = [
    "UserResponse",
    "RegisterRequest",
    "RegisterResponse",
    "LoginRequest",
    "LoginResponse",
    "SendCodeRequest",
    "ResetPasswordRequest",
    "ChangePasswordRequest",
    "ProfileUpdateRequest",
    "ChangeEmailSendCodeRequest",
    "ChangeEmailRequest",
]