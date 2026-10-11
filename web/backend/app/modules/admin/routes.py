"""管理员后台 API

权限：除 `GET /announcement`（所有登录用户可读）外，全部要求 admin 角色。
审计：所有写操作都会调用 record_audit 留痕。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import hash_password
from app.models.user import User
from app.modules.admin import services as svc
from app.modules.admin.audit import record_audit
from app.modules.auth.routes import get_current_user_required
from app.modules.permissions import ROLE_ADMIN, ROLE_USER, require_role

router = APIRouter(prefix="/api/v1/admin", tags=["系统管理"])


# ============================================================
# 通用
# ============================================================

def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (
        request.client.host if request.client else None
    )


def _ok(data):
    return {"success": True, "data": data, "error": None}


def _utc_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """把客户端传来的时间统一成与库一致的 UTC naive"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class UserOut(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleUpdateRequest(BaseModel):
    role: str = Field(..., pattern=f"^({ROLE_ADMIN}|{ROLE_USER})$")


class ActiveUpdateRequest(BaseModel):
    is_active: bool


class PasswordResetRequest(BaseModel):
    new_password: str = Field(..., min_length=6, description="新密码（至少 6 位）")


class BindEmailRequest(BaseModel):
    email: str = Field(..., min_length=3, description="要绑定的邮箱")


class SettingUpdateRequest(BaseModel):
    value: object = Field(..., description="设置项新值")


# ============================================================
# 管理总览
# ============================================================

@router.get("/overview", response_model=dict)
def get_overview(
    days: int = Query(30, ge=7, le=90, description="趋势窗口天数"),
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """管理总览：关键指标 + 趋势 + Top 榜单 + 异常提示"""
    return _ok(svc.get_overview(db, days=days))


# ============================================================
# 用量与成本
# ============================================================

@router.get("/usage", response_model=dict)
def get_usage(
    dimension: str = Query("operation", description="user | operation | model | day"),
    days: Optional[int] = Query(None, ge=1, le=365, description="只看最近 N 天"),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """按 用户 / 功能 / 模型 / 日期 维度统计用量与成本"""
    try:
        if days:
            start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        data = svc.get_usage(
            db, dimension=dimension,
            start=_utc_naive(start), end=_utc_naive(end), limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _ok(data)


@router.get("/usage/traces", response_model=dict)
def list_usage_traces(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: Optional[str] = None,
    operation: Optional[str] = None,
    trace_status: Optional[str] = Query(None, alias="status", description="success | failed"),
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """逐次调用明细"""
    return _ok(svc.list_traces(db, page=page, page_size=page_size,
                               user_id=user_id, operation=operation, status=trace_status))


# ============================================================
# 用户管理
# ============================================================

@router.get("/users", response_model=dict)
def list_users(
    q: Optional[str] = Query(None, description="按邮箱/昵称/id 搜索"),
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    byok: Optional[bool] = Query(None, description="是否自带 API Key"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """用户列表（支持搜索/筛选/分页，附带每人用量汇总）"""
    return _ok(svc.list_users_admin(db, q=q, role=role, is_active=is_active,
                                    byok=byok, page=page, page_size=page_size))


@router.get("/users/export", response_class=Response)
def export_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """导出全部用户（含用量）为 CSV"""
    csv_text = svc.export_users_csv(db)
    return Response(
        content="\ufeff" + csv_text,  # BOM 让 Excel 正确识别 UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="offerflow_users.csv"'},
    )


@router.get("/users/{user_id}/detail", response_model=dict)
def get_user_detail(
    user_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """用户详情：账号信息 + 用量 + 趋势 + 内容量"""
    data = svc.get_user_detail(db, user_id)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return _ok(data)


@router.put("/users/{user_id}/role", response_model=dict)
def update_user_role(
    user_id: str,
    req: RoleUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.id == current_user.id and req.role != ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能取消自己的管理员角色")

    old_role = user.role
    user.role = req.role
    db.commit()
    db.refresh(user)

    record_audit(current_user, "update_role", "user", user.id, user.email or user.name,
                 detail={"字段": "role", "旧值": old_role, "新值": req.role},
                 ip=_client_ip(request))
    return _ok(UserOut.model_validate(user))


@router.put("/users/{user_id}/active", response_model=dict)
def update_user_active(
    user_id: str,
    req: ActiveUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.id == current_user.id and not req.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能禁用当前管理员账号")

    old = bool(user.is_active)
    user.is_active = req.is_active
    db.commit()
    db.refresh(user)

    record_audit(current_user, "toggle_active", "user", user.id, user.email or user.name,
                 detail={"字段": "is_active", "旧值": old, "新值": req.is_active},
                 ip=_client_ip(request))
    return _ok(UserOut.model_validate(user))


@router.post("/users/{user_id}/reset_password", response_model=dict)
def reset_user_password(
    user_id: str,
    req: PasswordResetRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    """管理员重置任意用户密码（忘记密码时的恢复入口）。"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    user.hashed_password = hash_password(req.new_password)
    db.commit()

    # 审计只记「重置了密码」，绝不记录密码明文
    record_audit(current_user, "reset_password", "user", user.id, user.email or user.name,
                 detail={"说明": "管理员重置了该用户密码"}, ip=_client_ip(request))
    return _ok({"id": user.id, "name": user.name})


@router.post("/users/{user_id}/bind_email", response_model=dict)
def bind_user_email(
    user_id: str,
    req: BindEmailRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    """为存量账号补绑/换绑邮箱（邮箱即唯一身份，无邮箱账号无法登录）"""
    email = req.email.strip().lower()
    # 极简邮箱格式校验，与 auth 模块保持同样的宽松度
    if "@" not in email or "." not in email.split("@")[-1] or " " in email:
        raise HTTPException(status_code=400, detail="请输入正确的邮箱")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    existing = db.query(User).filter(User.email == email, User.id != user_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="该邮箱已被其他账号占用")

    old = user.email
    user.email = email
    db.commit()
    db.refresh(user)

    record_audit(current_user, "bind_email", "user", user.id, user.name,
                 detail={"字段": "email", "旧值": old, "新值": email},
                 ip=_client_ip(request))
    return _ok(UserOut.model_validate(user))


# ============================================================
# 系统健康
# ============================================================

@router.get("/health", response_model=dict)
def get_system_health(
    recent: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """系统健康：数据库、平台默认 Key、调用量、最近失败与最慢调用"""
    return _ok(svc.get_health(db, recent=recent))


# ============================================================
# 审计日志
# ============================================================

@router.get("/audit-logs", response_model=dict)
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    action: Optional[str] = None,
    operator_id: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """管理员操作审计日志"""
    return _ok(svc.list_audit_logs(db, page=page, page_size=page_size,
                                   action=action, operator_id=operator_id))


# ============================================================
# 系统设置
# ============================================================

@router.get("/settings", response_model=dict)
def get_settings(
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    """读取全部系统设置（含平台默认模型信息）"""
    settings_map = svc.get_settings_map(db)

    platform = {"model": None, "key_configured": False}
    try:
        from app.config import get_settings as _gs
        s = _gs()
        platform = {
            "model": s.llm_model,
            "key_configured": bool(s.deepseek_api_key),
            "base_url": s.deepseek_base_url,
        }
    except Exception:  # noqa: BLE001
        pass

    return _ok({"settings": settings_map, "platform_model": platform})


@router.put("/settings/{key}", response_model=dict)
def update_setting(
    key: str,
    req: SettingUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    """更新单个系统设置项"""
    try:
        result = svc.update_setting(db, key, req.value, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    record_audit(current_user, "update_setting", "setting", key, key,
                 detail={"字段": key, "旧值": result["old"], "新值": result["new"]},
                 ip=_client_ip(request))
    return _ok({"key": key, **result})


@router.get("/announcement", response_model=dict)
def get_announcement(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user_required),
):
    """平台公告（所有登录用户可读，用于客户端展示）"""
    enabled = bool(svc.get_setting_value(db, "announcement_enabled", False))
    content = svc.get_setting_value(db, "announcement", "") or ""
    return _ok({"enabled": enabled and bool(content.strip()), "content": content})
