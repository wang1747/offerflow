"""项目建议反馈路由：提交、公开列表、我的、点赞、管理员处理。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.auth.routes import get_current_user_required
from app.modules.permissions import ROLE_ADMIN, require_role
from app.models.user import User
from app.modules.feedback import services as svc

router = APIRouter(prefix="/api/v1/feedback", tags=["建议反馈模块"])


class CreateFeedbackRequest(BaseModel):
    category: str = Field(default="功能建议")
    title: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1)


class AdminUpdateRequest(BaseModel):
    status: Optional[str] = None
    admin_reply: Optional[str] = None


@router.post("")
async def create_feedback(
    req: CreateFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    fb = svc.create_feedback(db, current_user.id, req.category, req.title, req.content)
    return {"success": True, "data": {"id": fb.id}, "error": None}


@router.get("")
async def list_feedbacks(
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    result = svc.list_feedbacks(db, category=category, status=status, page=page, page_size=page_size)
    return {"success": True, "data": result, "error": None}


@router.get("/mine")
async def list_my_feedbacks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    data = svc.list_my_feedbacks(db, current_user.id)
    return {"success": True, "data": data, "error": None}


@router.post("/{feedback_id}/like")
async def toggle_like(
    feedback_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        result = svc.toggle_like(db, feedback_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"success": True, "data": result, "error": None}


@router.patch("/{feedback_id}")
async def admin_update(
    feedback_id: str,
    req: AdminUpdateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    fb = svc.admin_update(db, feedback_id, req.status, req.admin_reply)
    if not fb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="建议不存在")
    return {"success": True, "data": {"id": fb.id, "status": fb.status}, "error": None}
