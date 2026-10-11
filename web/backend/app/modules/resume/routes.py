import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.concurrency import run_sync
from app.core.database import get_db
from app.models.user import User
from app.modules.auth.routes import get_current_user_required
from app.modules.resume.export_service import (
    LAYOUT_CHOICES,
    export_to_pdf,
    export_to_word,
    resolve_layout,
    source_photo_bytes,
)
from app.modules.resume.parser import extract_docx_photo, parse_resume_bytes
from app.modules.resume.services import (
    delete_all_resumes,
    delete_resume,
    ensure_resume_structured,
    export_resume_data,
    get_resume_versions,
    list_resumes,
    optimize_resume_flow,
    rollback_to_version,
    upload_resume,
)
from app.modules.resume.versioning import get_document_versions

router = APIRouter()


class ResumeUploadRequest(BaseModel):
    raw_text: str
    source_file: Optional[str] = None
    # 桌面版离线创建时由客户端生成；同用户同 id 幂等（见 services.upload_resume）。
    id: Optional[str] = None


class ResumeOptimizeRequest(BaseModel):
    resume_id: str
    jd_text: str = ""
    jd_id: Optional[str] = None


@router.get("/list")
async def get_resume_list(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    resumes = list_resumes(db, user_id=current_user.id)
    return {"success": True, "data": resumes, "error": None}


@router.get("/layouts")
async def get_export_layouts():
    """导出可选版式清单（单栏 / 双栏）。

    放在 `/{resume_id}/...` 之前声明，避免被动态段吃掉。
    UI 的选项文案从这一处来，不要在页面里再手抄一份。
    """
    return {"success": True, "data": LAYOUT_CHOICES, "error": None}


@router.get("/export-data")
async def export_resume_data_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """导出当前用户的全部简历数据（JSON 文件下载），用于数据备份与隐私掌控。"""
    data = export_resume_data(db, user_id=current_user.id)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(data),
        "resumes": data,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=offerflow_resumes.json"},
    )


@router.delete("/all")
async def delete_all_resumes_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """删除当前用户的全部简历（不可恢复，调用前应确认）。"""
    deleted = delete_all_resumes(db, user_id=current_user.id)
    return {"success": True, "data": {"deleted": deleted}, "error": None}


@router.post("/upload")
async def upload_resume_endpoint(
    req: ResumeUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        resume = upload_resume(
            raw_text=req.raw_text,
            source_file=req.source_file,
            db=db,
            user_id=current_user.id,
            client_id=req.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "data": {"id": resume.id, "version": resume.version},
        "error": None,
    }


@router.post("/upload-file")
async def upload_resume_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件大小不能超过 10MB")

    try:
        raw_text = parse_resume_bytes(file.filename, content)
        parsed_extra = {}
        if (file.filename or "").lower().endswith(".docx"):
            photo = extract_docx_photo(content)
            if photo:
                parsed_extra["source_photo"] = photo
        resume = upload_resume(
            raw_text=raw_text,
            source_file=file.filename,
            db=db,
            user_id=current_user.id,
            parsed_extra=parsed_extra,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "success": True,
        "data": {
            "id": resume.id,
            "version": resume.version,
            "filename": file.filename,
        },
        "error": None,
    }


@router.get("/{resume_id}/versions")
async def get_resume_versions_endpoint(
    resume_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    versions = get_resume_versions(resume_id, db, user_id=current_user.id)
    if versions is None:
        raise HTTPException(status_code=404, detail="简历不存在")
    return {
        "success": True,
        "data": [
            {
                "id": v.id,
                "version": v.version,
                "raw_text": v.raw_text,
                "source_file": v.source_file,
                "parsed_json": v.parsed_json,
                "created_at": v.created_at,
            }
            for v in versions
        ],
        "error": None,
    }


@router.post("/{resume_id}/versions/{version_id}/rollback")
async def rollback_resume_version_endpoint(
    resume_id: str,
    version_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """把指定旧版本恢复为当前最新版本（复制旧版本内容，创建一条新版本）。"""
    try:
        resume = rollback_to_version(resume_id, version_id, db, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "success": True,
        "data": {"id": resume.id, "version": resume.version},
        "error": None,
    }


@router.post("/optimize")
async def optimize_resume_endpoint(
    req: ResumeOptimizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        result = await optimize_resume_flow(
            req.resume_id,
            req.jd_text,
            db,
            current_user,
            jd_id=req.jd_id,
        )
    except ValueError as exc:
        if str(exc) == "resume_not_found":
            raise HTTPException(status_code=404, detail="简历不存在")
        if str(exc) == "jd_not_found":
            raise HTTPException(status_code=404, detail="目标岗位不存在")
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/{resume_id}/export")
async def export_resume(
    resume_id: str,
    format: str = "pdf",
    layout: str = "single",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    from app.models.resume import Resume

    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == current_user.id,
    ).first()
    if not resume:
        raise HTTPException(status_code=404, detail="简历不存在")

    # 版式参数先收敛再校验：未知取值静默回退单栏（与导出层同一套规则），
    # 但「双栏 + Word」明确报错，不做「用户选了双栏、拿到的却是单栏」的静默降级
    # ——Word 的多栏要动 w:cols 与分节符，跨 Word/WPS 表现不一致，暂不支持。
    layout_key, _sidebar = resolve_layout(layout)
    if layout_key != "single" and format.lower() in ("word", "docx"):
        raise HTTPException(
            status_code=400,
            detail="Word 导出暂不支持多栏版式，请改用 PDF 或选择单栏",
        )

    # 首次导出时惰性补结构化数据：上传/粘贴的简历缺这一段，
    # 不补的话导出会丢掉标题行 / bullet / 时间对齐等排版层次。
    # **只在内存里补，不写库** —— 导出是只读动作，写库会改到历史版本（版本不可变）。
    # 提取失败不影响导出（退回纯文本渲染），也不阻塞响应语义。
    # ensure_resume_structured 内含同步 DB 查询 + LLM 调用，整体丢线程池避免占死事件循环。
    structured = await run_sync(ensure_resume_structured, resume, db, current_user)

    # 兼容早期已经生成、但没有继承 source_photo 的优化版本：只要同一文档的
    # 任意历史版本仍有原图，就在内存里恢复使用，不修改历史记录。
    photo = source_photo_bytes(resume)
    if photo is None:
        for version in get_document_versions(db, current_user.id, resume):
            photo = source_photo_bytes(version)
            if photo is not None:
                break

    fmt = format.lower()
    if fmt == "pdf":
        content = export_to_pdf(resume, structured, photo, layout=layout_key)
        media_type = "application/pdf"
        filename = f"resume_{resume_id}.pdf"
    elif fmt in ("word", "docx"):
        content = export_to_word(resume, structured, photo)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        filename = f"resume_{resume_id}.docx"
    else:
        raise HTTPException(status_code=400, detail="不支持的格式，请使用 pdf 或 word")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.delete("/{resume_id}")
async def delete_resume_endpoint(
    resume_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """删除指定简历（不可恢复）。"""
    deleted = delete_resume(resume_id, db, user_id=current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="简历不存在")
    return {"success": True, "data": {"deleted": deleted}, "error": None}
