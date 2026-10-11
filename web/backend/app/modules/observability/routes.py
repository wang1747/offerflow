"""可观测查询路由：trace 列表 / 详情 / 成本汇总"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.auth.routes import get_current_user_required
from app.modules.observability.services import (
    get_summary,
    get_trace_detail,
    list_traces,
)
from app.models.user import User
from app.schemas.common import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/observability", tags=["可观测模块"])


@router.get("/summary", summary="获取成本汇总", response_model=ApiResponse[dict])
async def summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """获取当前用户的成本汇总（总费用/token、按操作、按模型聚合）"""
    try:
        data = get_summary(db, user_id=current_user.id)
        return ApiResponse(success=True, data=data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取成本汇总失败: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取成本汇总失败")


@router.get("/traces", summary="获取 trace 列表", response_model=ApiResponse[list])
async def traces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    operation: Optional[str] = Query(None, description="按操作类型筛选"),
):
    """获取当前用户的 trace 列表（分页）"""
    try:
        data = list_traces(db, user_id=current_user.id, page=page, page_size=page_size, operation=operation)
        return ApiResponse(success=True, data=data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取 trace 列表失败: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取 trace 列表失败")


@router.get("/traces/{trace_id}", summary="获取 trace 详情", response_model=ApiResponse[dict])
async def trace_detail(
    trace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """获取单条 trace 详情（含全部 spans）"""
    try:
        data = get_trace_detail(db, user_id=current_user.id, trace_id=trace_id)
        if not data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace 不存在")
        return ApiResponse(success=True, data=data)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取 trace 详情失败: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取 trace 详情失败")
