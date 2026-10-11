"""社区分享路由：发帖、列表、详情、评论、点赞、删除、附件上传。"""

import os
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.auth.routes import get_current_user_required
from app.modules.permissions import ROLE_ADMIN, require_role
from app.models.user import User
from app.modules.community import moderation, services as svc

router = APIRouter(prefix="/api/v1/community", tags=["社区交流模块"])

# 附件存储目录（挂载 upload-data volume，与语料/简历同盘）
UPLOAD_DIR = os.environ.get("COMMUNITY_UPLOAD_DIR", "/app/uploads/community")

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_DOC_EXT = {".doc", ".docx", ".pdf"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024      # 5MB
MAX_DOC_SIZE = 10 * 1024 * 1024       # 10MB

# 文件名：uuid.hex(32) + 已知扩展，杜绝路径穿越
_FILE_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp|doc|docx|pdf)$")


class CreatePostRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1)
    category: str = Field(default="经验分享")
    tags: Optional[List[str]] = None
    attachments: Optional[List[Dict[str, Any]]] = None


class CreateCommentRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


@router.post("/posts")
async def create_post(
    req: CreatePostRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    hit = moderation.moderate_fields(req.title, req.content)
    if hit:
        raise HTTPException(status_code=400, detail="内容包含不当用语，请修改后重试")
    post = svc.create_post(
        db, current_user.id, req.title, req.content, req.category, req.tags,
        attachments=req.attachments,
    )
    return {"success": True, "data": {"id": post.id}, "error": None}


@router.get("/posts")
async def list_posts(
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    result = svc.list_posts(db, category=category, page=page, page_size=page_size)
    return {"success": True, "data": result, "error": None}


@router.get("/posts/{post_id}")
async def get_post(
    post_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    detail = svc.get_post_detail(db, post_id)
    if not detail:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="帖子不存在")
    return {"success": True, "data": detail, "error": None}


@router.post("/posts/{post_id}/comments")
async def add_comment(
    post_id: str,
    req: CreateCommentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if moderation.moderate(req.content):
        raise HTTPException(status_code=400, detail="评论包含不当用语，请修改后重试")
    try:
        comment = svc.add_comment(db, post_id, current_user.id, req.content)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"success": True, "data": {"id": comment.id}, "error": None}


@router.post("/posts/{post_id}/like")
async def toggle_like(
    post_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        result = svc.toggle_like(db, post_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"success": True, "data": result, "error": None}


@router.delete("/posts/{post_id}")
async def delete_post(
    post_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    is_admin = current_user.role == ROLE_ADMIN
    try:
        ok = svc.delete_post(db, post_id, current_user.id, is_admin=is_admin)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="帖子不存在")
    return {"success": True, "data": None, "error": None}


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    ok = svc.delete_comment(db, comment_id, is_admin=True)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评论不存在")
    return {"success": True, "data": None, "error": None}


@router.post("/uploads")
async def upload_attachment(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user_required),
):
    """上传帖子附件：图片（jpg/png/gif/webp）或 Word/PDF 文档。"""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ALLOWED_IMAGE_EXT:
        kind = "image"
        max_size = MAX_IMAGE_SIZE
    elif ext in ALLOWED_DOC_EXT:
        kind = "doc"
        max_size = MAX_DOC_SIZE
    else:
        raise HTTPException(
            status_code=400,
            detail="仅支持图片（jpg/png/gif/webp）或 Word/PDF 文档",
        )

    content = await file.read()
    if len(content) > max_size:
        limit_mb = max_size // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"{'图片' if kind == 'image' else '文档'}大小不能超过 {limit_mb}MB",
        )

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(UPLOAD_DIR, stored_name), "wb") as fh:
        fh.write(content)

    return {
        "success": True,
        "data": {
            "name": stored_name,
            "kind": kind,
            "original_name": filename,
            "size": len(content),
            "url": f"/api/v1/community/files/{stored_name}",
        },
        "error": None,
    }


@router.get("/files/{name}")
async def get_attachment(name: str):
    """访问附件文件。文件名随机 uuid 不可枚举 + 白名单扩展名，无需登录即可读
    （否则 <img>/<a> 直接访问带不上 Authorization 会 401）。"""
    if not _FILE_RE.match(name.lower()):
        raise HTTPException(status_code=404, detail="文件不存在")
    path = os.path.join(UPLOAD_DIR, name.lower())
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)
