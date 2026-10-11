"""
通用响应模型
"""

from typing import Generic, TypeVar, Optional

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    data: Optional[T] = None
    error: Optional[str] = None


def success_response(data: Optional[T] = None) -> ApiResponse[T]:
    """成功响应"""
    return ApiResponse(success=True, data=data, error=None)


def error_response(error: str) -> ApiResponse:
    """错误响应"""
    return ApiResponse(success=False, data=None, error=error)