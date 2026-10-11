"""文档解析与图谱检索路由。

- POST /api/v1/parse/document   上传文件 -> Markdown（走 doc_parse 三件套，降级基础提取）
- GET  /api/v1/parse/status     查看当前解析器 / 图谱可用性
- POST /api/v1/parse/graph/query  图谱多跳检索（LightRAG，降级向量检索）
- POST /api/v1/parse/graph/insert 向图谱插入文档（需登录）

解析、图谱检索和状态接口都需要登录，避免匿名触发高成本解析或检索。
"""
import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.doc_parse import parse_to_markdown, parser_status
from app.core.graph_rag import query_graph, insert_text, graph_status
from app.modules.auth.routes import get_current_user_required
from app.modules.permissions import ROLE_ADMIN
from app.models.user import User

router = APIRouter()
MAX_PARSE_BYTES = 20 * 1024 * 1024
_PARSE_SEMAPHORE = asyncio.Semaphore(2)


class GraphQueryRequest(BaseModel):
    query_text: str
    top_k: int = 5
    mode: str = "hybrid"  # hybrid / local / global / naive


class GraphInsertRequest(BaseModel):
    text: str


@router.post("/document")
async def parse_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user_required),
):
    """上传文件，返回解析后的 Markdown。"""
    async with _PARSE_SEMAPHORE:
        data = await file.read(MAX_PARSE_BYTES + 1)
        if len(data) > MAX_PARSE_BYTES:
            raise HTTPException(status_code=413, detail="文件不能超过 20MB")
        md = await run_in_threadpool(parse_to_markdown, data, file.filename or "doc.bin")
    return {
        "success": True,
        "data": {"markdown": md, "filename": file.filename, "length": len(md)},
        "error": None,
    }


@router.get("/status")
async def status(current_user: User = Depends(get_current_user_required)):
    return {
        "success": True,
        "data": {"doc_parse": parser_status(), "graph_rag": graph_status()},
        "error": None,
    }


@router.post("/graph/query")
async def graph_query(
    req: GraphQueryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    req.top_k = max(1, min(req.top_k, 20))
    results = await run_in_threadpool(
        query_graph,
        req.query_text,
        req.top_k,
        req.mode,
        db,
        current_user.id,
        True,
        current_user.role == ROLE_ADMIN,
    )
    return {"success": True, "data": results, "error": None}


@router.post("/graph/insert")
async def graph_insert(
    req: GraphInsertRequest,
    current_user: User = Depends(get_current_user_required),
):
    ok = await run_in_threadpool(insert_text, req.text)
    return {"success": True, "data": {"inserted": ok}, "error": None}
