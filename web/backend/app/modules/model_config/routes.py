"""
模型配置路由：用户自带 API Key（BYOK）
"""

# ===== 标准库 =====
import logging

# ===== 第三方库 =====
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

# ===== 项目内部 =====
from app.core.database import get_db
from app.modules.auth.routes import get_current_user_required
from app.models.user import User
from app.modules.model_config.services import (
    get_user_model_config,
    save_model_config,
    clear_model_config,
    test_model_connection,
    get_preset_providers,
    validate_base_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/user/model-config", tags=["模型设置"])


# ============ 请求/响应模型 ============

class SaveModelConfigRequest(BaseModel):
    provider: str = Field(..., min_length=1, description="模型提供商", examples=["deepseek"])
    base_url: str = Field(..., min_length=1, description="API Base URL", examples=["https://api.deepseek.com"])
    model: str = Field(..., min_length=1, description="模型名称", examples=["deepseek-flash"])
    api_key: str = Field(..., min_length=1, description="API Key")


class TestConnectionRequest(BaseModel):
    base_url: str = Field(..., min_length=1, description="API Base URL")
    api_key: str = Field(..., min_length=1, description="API Key")
    model: str = Field(..., min_length=1, description="模型名称")


class ModelConfigResponse(BaseModel):
    provider: str
    base_url: str
    model: str
    api_key_masked: str
    has_config: bool


class PresetProviderResponse(BaseModel):
    key: str
    base_url: str
    model: str


# ============ 接口 ============

@router.get(
    "/presets",
    summary="获取预设模型列表",
    description="返回支持的模型预设列表，包含默认 Base URL 和模型名"
)
async def get_presets():
    """获取预设模型列表"""
    presets = get_preset_providers()
    return {
        "success": True,
        "data": [
            {"key": key, "base_url": value["base_url"], "model": value["model"]}
            for key, value in presets.items()
        ],
        "error": None
    }


@router.get("")
@router.get(
    "/",
    summary="获取当前用户模型配置",
    description="获取当前用户的模型配置（API Key 仅返回掩码）"
)
async def get_model_config(
    current_user: User = Depends(get_current_user_required)
):
    """获取当前用户的模型配置"""
    config = get_user_model_config(current_user)
    return {
        "success": True,
        "data": config,
        "error": None
    }


@router.post("")
@router.post(
    "/",
    summary="保存模型配置",
    description="保存当前用户的模型配置（API Key 加密存储）"
)
async def save_model_config_endpoint(
    req: SaveModelConfigRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    """保存模型配置"""
    try:
        # 校验 Base URL
        if not validate_base_url(req.base_url):
            raise ValueError("无效的 Base URL，请检查地址格式")

        # 先测试连接
        test_result = test_model_connection(
            base_url=req.base_url,
            api_key=req.api_key,
            model=req.model
        )
        if not test_result.get("success"):
            raise ValueError(f"连接测试失败：{test_result.get('error', '未知错误')}")

        # 保存配置
        save_model_config(
            user=current_user,
            provider=req.provider,
            base_url=req.base_url,
            model=req.model,
            api_key=req.api_key,
            db=db
        )

        logger.info(f"用户 {current_user.id} 更新了模型配置: provider={req.provider}")

        return {
            "success": True,
            "data": {
                "message": "模型配置保存成功",
                "config": get_user_model_config(current_user)
            },
            "error": None
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        logger.error("保存模型配置失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="保存配置失败，请稍后重试"
        )


@router.post(
    "/test",
    summary="测试模型连接",
    description="测试指定的模型配置是否可用"
)
async def test_connection(
    req: TestConnectionRequest,
    current_user: User = Depends(get_current_user_required)
):
    """测试模型连接"""
    try:
        if not validate_base_url(req.base_url):
            return {
                "success": False,
                "data": None,
                "error": "无效的 Base URL，请检查地址格式"
            }

        result = test_model_connection(
            base_url=req.base_url,
            api_key=req.api_key,
            model=req.model
        )
        return {
            "success": result.get("success", False),
            "data": result if result.get("success") else None,
            "error": result.get("error") if not result.get("success") else None
        }
    except Exception:
        logger.error("测试连接失败")
        return {
            "success": False,
            "data": None,
            "error": "连接测试失败，请检查网络或配置"
        }


@router.delete("")
@router.delete(
    "/",
    summary="清除模型配置",
    description="清除当前用户的模型配置，LLM 功能将不可用"
)
async def clear_model_config_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    """清除模型配置"""
    clear_model_config(current_user, db)

    logger.info(f"用户 {current_user.id} 清除了模型配置")

    return {
        "success": True,
        "data": {"message": "模型配置已清除，LLM 功能将不可用"},
        "error": None
    }
