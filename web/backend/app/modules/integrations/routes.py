"""集成能力聚合与验证路由（A 类 mem0/headroom + 文档解析 + 图谱）。

- GET  /api/v1/integrations/status         聚合所有新功能状态 + 机器内存
- POST /api/v1/integrations/memory/add     写入一条记忆（降级：进程内 dict）
- POST /api/v1/integrations/memory/search  检索记忆（降级：进程内 dict）
- POST /api/v1/integrations/compress       压缩文本（降级：截断）

说明：A 类（mem0/headroom）在 1.6GB 小机上默认走优雅降级（进程内记忆 / 截断），
真实后端需 qdrant+OpenAI / headroom 库，本机未部署重依赖，故验证降级路径。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.doc_parse import parser_status
from app.core.graph_rag import graph_status
from app.core.memory import (
    memory_status,
    add as mem_add,
    search as mem_search,
    recall_as_text,
)
from app.core.compress import (
    compress_status,
    compress_text,
    compress_chunks,
)
from app.models.user import User
from app.modules.auth.routes import get_current_user_required

router = APIRouter()


class MemAddReq(BaseModel):
    text: str


class MemSearchReq(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=20)


class CompressReq(BaseModel):
    text: str = ""
    chunks: list = []
    max_chars: int = Field(default=4000, ge=100, le=20000)


@router.get("/status")
async def status(current_user: User = Depends(get_current_user_required)):
    return {
        "success": True,
        "data": {
            "doc_parse": parser_status(),
            "graph_rag": graph_status(),
            "mem0": memory_status(),
            "headroom": compress_status(),
        },
        "error": None,
    }


@router.post("/memory/add")
async def memory_add(
    req: MemAddReq,
    current_user: User = Depends(get_current_user_required),
):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    mem_add(current_user.id, req.text)
    return {"success": True, "data": {"added": True}, "error": None}


@router.post("/memory/search")
async def memory_search(
    req: MemSearchReq,
    current_user: User = Depends(get_current_user_required),
):
    items = mem_search(current_user.id, req.query, req.limit)
    return {
        "success": True,
        "data": {
            "items": items,
            "recall_as_text": recall_as_text(current_user.id, req.query, req.limit),
        },
        "error": None,
    }


@router.post("/compress")
async def compress(
    req: CompressReq,
    current_user: User = Depends(get_current_user_required),
):
    if req.chunks:
        out = compress_chunks(req.chunks, req.max_chars)
    else:
        out = compress_text(req.text, req.max_chars)
    return {"success": True, "data": {"result": out}, "error": None}
