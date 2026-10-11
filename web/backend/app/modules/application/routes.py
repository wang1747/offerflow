"""
投递追踪路由：CRUD、状态更新、统计
"""

# ===== 标准库 =====
import logging
from datetime import date, datetime
from typing import Optional, Literal

# ===== 第三方库 =====
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

# ===== 项目内部 =====
from app.core.database import get_db
from app.modules.application.services import (
    add_event,
    create_application,
    delete_application,
    get_application,
    get_statistics,
    list_applications,
    update_application,
    update_application_status,
)
from app.modules.application.reminder_service import get_company_interview_articles, get_reminders
from app.modules.auth.routes import get_current_user_required
from app.models.user import User
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/applications", tags=["投递追踪模块"])


class CreateApplicationRequest(BaseModel):
    company: str = Field(..., min_length=1)
    position: str = Field(..., min_length=1)
    jd_id: Optional[str] = None
    resume_id: Optional[str] = None
    # 桌面版离线创建时由客户端生成；服务端保证「同用户 + 同 id」只建一条（幂等，
    # 见 services.create_application），这样离线队列重复上行不会产生重复数据。
    id: Optional[str] = Field(None, description="客户端生成的 id（离线同步幂等用）")


class UpdateApplicationRequest(BaseModel):
    status: Optional[
        Literal["saved", "applied", "online_test", "first_interview", "second_interview", "hr_round", "offered", "accepted", "rejected"]
    ] = None
    applied_date: Optional[date] = None
    next_action: Optional[str] = None
    next_action_date: Optional[date] = None
    notes: Optional[str] = None


class UpdateStatusRequest(BaseModel):
    status: Literal["saved", "applied", "online_test", "first_interview", "second_interview", "hr_round", "offered", "accepted", "rejected"] = Field(...)


class EventCreateRequest(BaseModel):
    event_type: str = Field(..., min_length=1)
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    description: Optional[str] = None


class ApplicationOut(BaseModel):
    id: str
    company: str
    position: str
    status: str
    jd_id: Optional[str]
    resume_id: Optional[str]
    applied_date: Optional[date]
    next_action: Optional[str]
    next_action_date: Optional[date]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.post("")
@router.post("/", summary="创建投递记录")
async def create_application_endpoint(
    req: CreateApplicationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    try:
        app = create_application(
            company=req.company,
            position=req.position,
            jd_id=req.jd_id,
            resume_id=req.resume_id,
            db=db,
            user_id=current_user.id,
            client_id=req.id,
        )
        logger.info(f"用户 {current_user.id} 创建投递记录: {app.id}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"success": True, "data": {"id": app.id}, "error": None}


@router.get("")
@router.get("/", summary="获取投递列表")
async def list_applications_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
    status: Optional[str] = Query(None, description="按状态筛选")
):
    apps = list_applications(db, user_id=current_user.id, status=status)
    return {"success": True, "data": [ApplicationOut.model_validate(item) for item in apps], "error": None}


@router.get("/stats", summary="获取投递统计数据")
async def get_statistics_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    stats = get_statistics(db, user_id=current_user.id)
    return {"success": True, "data": stats, "error": None}


@router.get("/reminders", summary="获取提醒汇总")
async def get_reminders_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    reminders = get_reminders(db, user_id=current_user.id)
    return {"success": True, "data": reminders, "error": None}


@router.get("/reminders/export", summary="导出提醒供 n8n 使用")
async def export_reminders_for_n8n(
    token: str = Query(..., description="服务令牌"),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if token != settings.reminder_service_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service token")

    users = db.query(User).all()
    data = []
    for user in users:
        reminders = get_reminders(db, user.id)
        for section in ("overdue", "upcoming"):
            for item in reminders.get(section, []):
                item["interview_articles"] = get_company_interview_articles(
                    item["company"],
                    db,
                    user.id,
                )
        data.append({
            "user": {"id": user.id, "name": user.name},
            "reminders": reminders,
        })
    return {"success": True, "data": data, "error": None}


@router.post("/{app_id}/events", summary="添加投递事件")
async def add_event_endpoint(
    app_id: str,
    req: EventCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    event = add_event(
        application_id=app_id,
        event_type=req.event_type,
        from_status=req.from_status,
        to_status=req.to_status,
        description=req.description,
        db=db,
        user_id=current_user.id
    )
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在")
    return {
        "success": True,
        "data": {
            "id": event.id,
            "event_type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "description": event.description,
            "event_date": event.event_date,
        },
        "error": None,
    }


@router.get("/{app_id}/interview-articles", summary="获取公司面经（面试前推送）")
async def get_interview_articles_for_application(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    app = get_application(app_id, db, user_id=current_user.id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在")
    articles = get_company_interview_articles(app.company, db, user_id=current_user.id)
    return {"success": True, "data": articles, "error": None}


@router.get("/{app_id}", summary="获取单个投递记录")
async def get_application_endpoint(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    app = get_application(app_id, db, user_id=current_user.id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在")
    return {"success": True, "data": ApplicationOut.model_validate(app), "error": None}


@router.put("/{app_id}", summary="更新投递记录")
async def update_application_endpoint(
    app_id: str,
    req: UpdateApplicationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    try:
        app = update_application(
            app_id=app_id,
            db=db,
            user_id=current_user.id,
            status=req.status,
            applied_date=req.applied_date,
            next_action=req.next_action,
            next_action_date=req.next_action_date,
            notes=req.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在")
    return {"success": True, "data": ApplicationOut.model_validate(app), "error": None}


@router.delete("/{app_id}", summary="删除投递记录")
async def delete_application_endpoint(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    if not delete_application(app_id, db, user_id=current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在")
    return {"success": True, "data": None, "error": None}


@router.put("/{app_id}/status", summary="更新投递状态")
async def update_status_endpoint(
    app_id: str,
    req: UpdateStatusRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    try:
        app = update_application_status(app_id, req.status, db, user_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在")
    logger.info(f"用户 {current_user.id} 更新投递状态: {app_id} → {req.status}")
    return {"success": True, "data": {"id": app.id, "status": app.status}, "error": None}
