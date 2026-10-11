from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from fastapi import File, UploadFile
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.modules.jd.services import parse_and_save, list_jds, delete_jd, update_jd
from app.modules.auth.routes import get_current_user_required
from app.models.user import User
from app.modules.jd.ocr_service import extract_text_smart

router = APIRouter()


class JDParseRequest(BaseModel):
    raw_text: str
    # 客户端（桌面版）生成的主键：离线新建后上行时带上，保证「同用户同 id 只建一条」。
    # 不传则沿用服务端 uuid 默认值（网页版行为不变）。
    id: Optional[str] = None


class JDUpdateRequest(BaseModel):
    company: Optional[str] = None
    position: Optional[str] = None


@router.get("/list")
async def list_jds_route(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    jds = list_jds(db, user_id=current_user.id)
    return {"success": True, "data": jds, "error": None}


@router.post("/parse")
async def parse_jd(
    req: JDParseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    result = await parse_and_save(raw_text=req.raw_text, db=db, user_id=current_user.id,
                                  client_id=req.id)
    return result

@router.post("/ocr")
async def ocr_import_jd(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    """
    通过截图导入 JD

    识别策略：**视觉大模型优先（理解版式，复杂 JD 效果好），失败自动降级本地 OCR**。
    响应中的 engine 字段标明本次实际使用的引擎（vision / ocr）。
    """
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件大小不能超过 10MB")

    try:
        raw_text, engine = extract_text_smart(
            content, file.filename or "image.png", user=current_user
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await parse_and_save(raw_text, db, user_id=current_user.id)

    if result.get("error"):
        return {"success": False, "data": None, "error": result["error"]}

    return {
        "success": True,
        "data": {
            "id": result["data"]["id"],
            "parsed": result["data"]["parsed"],
            "ocr_text": raw_text[:500] + ("..." if len(raw_text) > 500 else ""),
            "engine": engine,
        },
        "error": None,
    }

@router.delete("/{jd_id}")
async def delete_jd_route(
    jd_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    if not delete_jd(jd_id, db, user_id=current_user.id):
        raise HTTPException(status_code=404, detail="职位不存在")
    return {"success": True, "data": None, "error": None}


@router.put("/{jd_id}")
async def update_jd_route(
    jd_id: str,
    req: JDUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    if req.company is None and req.position is None:
        raise HTTPException(status_code=400, detail="请至少填写公司或职位")
    jd = update_jd(
        jd_id,
        db=db,
        user_id=current_user.id,
        company=req.company,
        position=req.position,
    )
    if not jd:
        raise HTTPException(status_code=404, detail="职位不存在")
    return {
        "success": True,
        "data": {"id": jd.id, "company": jd.company, "position": jd.position},
        "error": None,
    }
