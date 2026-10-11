"""
统一异常处理
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
import logging

logger = logging.getLogger(__name__)


class QuotaExceededError(ValueError):
    """
    平台每日用量配额已用尽（用户未自带 Key 且当日 token 消耗达到上限）。

    继承 ValueError 是刻意的：业务代码里普遍用 `except ValueError as e: return str(e)`
    来保留错误信息（如简历生成、JD 分析），继承它能让配额提示自然透传；
    若继承 Exception，则会落入 `except Exception` 的笼统兜底（如「生成失败，请稍后重试」），
    把配额提示吞掉（曾踩过）。
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message or "今日 AI 用量已达上限")


def register_exception_handlers(app: FastAPI):
    """注册全局异常处理器"""

    @app.exception_handler(QuotaExceededError)
    async def quota_exceeded_handler(request: Request, exc: QuotaExceededError):
        logger.info(f"配额超限: {exc}")
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "success": False,
                "data": None,
                "error": exc.message or "今日 AI 用量已达上限，可配置自己的 API Key 继续使用",
                "code": "quota_exceeded",
            }
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        logger.error(f"未捕获异常: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "data": None,
                "error": "服务器内部错误，请稍后重试"
            }
        )

    @app.exception_handler(SQLAlchemyError)
    async def db_exception_handler(request: Request, exc: SQLAlchemyError):
        logger.error(f"数据库异常: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "data": None,
                "error": "数据库操作失败，请稍后重试"
            }
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        logger.warning(f"参数错误: {exc}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "data": None,
                "error": str(exc)
            }
        )