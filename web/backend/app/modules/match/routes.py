"""
匹配路由：计算匹配度、匹配详情、排名列表
"""

# ===== 标准库 =====
import logging
from typing import List

# ===== 第三方库 =====
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# ===== 项目内部 =====
from app.core.database import get_db
from app.modules.match.services import calculate_match, get_match_detail, get_rankings
from app.modules.jd.services import parse_and_save
from app.modules.auth.routes import get_current_user_required
from app.models.user import User
from app.schemas.match import MatchRequest, MatchResponse, RankingItem
from app.schemas.common import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/match", tags=["匹配分析模块"])


class BatchMatchRequest(BaseModel):
    resume_id: str = Field(..., min_length=1)
    jd_texts: List[str] = Field(..., min_length=1)


@router.post("")
@router.post(
    "/",
    summary="计算匹配度",
    response_model=ApiResponse[MatchResponse],
    description="计算指定 JD 与简历的匹配度，返回评分、技能匹配详情、差距分析和建议"
)
async def match(
    req: MatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    try:
        result = calculate_match(req.jd_id, req.resume_id, db, user_id=current_user.id)
        logger.info(f"用户 {current_user.id} 匹配计算完成: jd={req.jd_id}, resume={req.resume_id}, score={result.get('score')}")
        return ApiResponse(success=True, data=MatchResponse.model_validate(result))
    except ValueError as e:
        logger.warning(f"匹配计算失败: {e}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"匹配计算未知异常: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="匹配计算失败，请稍后重试")


@router.post(
    "/batch",
    summary="批量匹配 JD",
    response_model=ApiResponse[dict],
    description="批量解析并匹配多个 JD，返回每个 JD 的匹配结果"
)
async def batch_match(
    req: BatchMatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    results = []
    for index, raw_text in enumerate(req.jd_texts, start=1):
        text = (raw_text or "").strip()
        if not text:
            continue
        try:
            parsed = await parse_and_save(raw_text=text, db=db, user_id=current_user.id)
            if not parsed.get("success") or not parsed.get("data"):
                results.append({
                    "index": index,
                    "success": False,
                    "error": parsed.get("error", "JD 解析失败"),
                })
                continue
            jd_id = parsed["data"]["id"]
            match_result = calculate_match(jd_id, req.resume_id, db, user_id=current_user.id)
            results.append({
                "index": index,
                "success": True,
                "match": MatchResponse.model_validate(match_result),
            })
        except ValueError as e:
            results.append({"index": index, "success": False, "error": str(e)})
        except Exception as e:
            logger.error(f"批量匹配异常: {e}")
            results.append({"index": index, "success": False, "error": "批量匹配失败"})

    return ApiResponse(success=True, data={"results": results}, error=None)


@router.get(
    "/rankings",
    summary="获取匹配排名",
    response_model=ApiResponse[List[RankingItem]],
    description="获取当前用户所有已匹配的 JD，按匹配度从高到低排序"
)
async def rankings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量")
):
    try:
        data = get_rankings(db, user_id=current_user.id, page=page, page_size=page_size)
        return ApiResponse(success=True, data=[RankingItem.model_validate(item) for item in data])
    except Exception as e:
        logger.error(f"获取排名列表失败: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取排名失败，请稍后重试")


@router.get(
    "/{match_id}",
    summary="获取匹配详情",
    response_model=ApiResponse[MatchResponse],
    description="根据匹配结果 ID 获取单条匹配详情"
)
async def match_detail(
    match_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    try:
        result = get_match_detail(match_id, db, user_id=current_user.id)
        if not result:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="匹配结果不存在")
        return ApiResponse(success=True, data=MatchResponse.model_validate(result))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取匹配详情失败: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取详情失败，请稍后重试")
