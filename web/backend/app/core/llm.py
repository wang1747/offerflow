"""
LLM 客户端：支持按用户动态创建
"""

from functools import lru_cache
from typing import Optional

from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.core.encryption import decrypt_value
from app.models.user import User
from app.observability.observer import get_observer


@lru_cache
def get_llm() -> ChatOpenAI:
    """
    获取全局 LLM 实例（用于 JD 解析等无需用户登录的场景）
    使用服务器配置的 DeepSeek API Key
    """
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise ValueError("服务器未配置默认 DeepSeek API Key")
    return ChatOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        model=settings.llm_model,
        temperature=0.1,
        callbacks=[get_observer()],
    )


def get_user_llm(user: User) -> Optional[ChatOpenAI]:
    """
    根据用户配置动态创建 LLM 实例

    如果用户没有配置模型，返回 None
    如果用户配置了模型，使用用户配置创建
    """
    if not user.encrypted_api_key or not user.llm_base_url or not user.llm_model:
        return None

    try:
        api_key = decrypt_value(user.encrypted_api_key)
        if not api_key:
            return None

        return ChatOpenAI(
            api_key=api_key,
            base_url=user.llm_base_url,
            model=user.llm_model,
            temperature=0.1,
            callbacks=[get_observer()],
        )
    except Exception:
        return None


def has_user_llm_config(user: User) -> bool:
    """检查用户是否配置了 LLM"""
    return bool(
        user.encrypted_api_key
        and user.llm_base_url
        and user.llm_model
    )


def get_user_llm_or_raise(user: User) -> ChatOpenAI:
    """
    获取用户 LLM 实例。

    优先使用用户自有配置（BYOK）；用户未配置时回退到服务器默认 Key，
    供测试/试用用户无需自备 Key 即可使用（默认 Key 仅存服务端，绝不下发前端）。

    消费平台默认 Key 前会做配额校验（仅对未自带 Key 的用户生效）。
    """
    llm = get_user_llm(user)
    if llm is None:
        settings = get_settings()
        if settings.deepseek_api_key:
            _ensure_quota(user)
            return get_llm()
        raise ValueError("请先完成模型设置")
    return llm


def _ensure_quota(user: User) -> None:
    """平台默认 Key 的每日配额校验；配额检查本身失败时放行（不阻断业务）"""
    try:
        from app.core.database import SessionLocal
        from app.core.exceptions import QuotaExceededError
        from app.modules.admin.services import check_quota

        db = SessionLocal()
        try:
            message = check_quota(db, user)
        finally:
            db.close()
        if message:
            raise QuotaExceededError(message)
    except QuotaExceededError:
        raise
    except Exception:  # noqa: BLE001
        return  # 配额查询异常不阻断
