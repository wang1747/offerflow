"""
模型配置服务：管理用户的自定义 LLM 配置
"""

import re
import socket
from ipaddress import ip_address
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.encryption import encrypt_value, decrypt_value, mask_api_key
from app.models.user import User


# 预设模型配置
PRESET_PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "qwen": {
        "base_url": "",  # 待确认
        "model": "",  # 待确认
    },
    "glm": {
        "base_url": "",  # 待确认
        "model": "",  # 待确认
    },
}


def validate_base_url(url: str) -> bool:
    """
    校验 Base URL：必须是 http/https，不能是内网地址（防 SSRF）
    """
    if not url:
        return False
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    host_lower = host.lower().rstrip(".")
    if (
        host_lower == "localhost"
        or host_lower.endswith((".local", ".internal", ".localhost", ".home.arpa"))
    ):
        return False

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False

    def is_public_address(addr) -> bool:
        return not (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        )

    try:
        return is_public_address(ip_address(host_lower))
    except ValueError:
        pass

    try:
        addresses = {ip_address(item[4][0]) for item in socket.getaddrinfo(
            host_lower,
            port,
            type=socket.SOCK_STREAM,
        )}
    except (OSError, ValueError):
        return False

    if not addresses:
        return False
    return all(is_public_address(addr) for addr in addresses)


def get_user_model_config(user: User) -> dict:
    """
    获取用户的模型配置（返回掩码 Key，不返回完整 Key）。

    has_config：用户是否配置了自有 Key（BYOK）。
    using_default：用户未配置自有 Key，但平台提供了默认 Key 可用（测试用户兜底）。
    """
    has_own = bool(
        user.llm_provider
        and user.llm_base_url
        and user.llm_model
        and user.encrypted_api_key
    )
    using_default = False
    if not has_own:
        from app.config import get_settings
        using_default = bool(get_settings().deepseek_api_key)

    return {
        "provider": user.llm_provider,
        "base_url": user.llm_base_url,
        "model": user.llm_model,
        "api_key_masked": mask_api_key(decrypt_value(user.encrypted_api_key)) if user.encrypted_api_key else "",
        "has_config": has_own,
        "using_default": using_default,
    }


def save_model_config(
    user: User,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    db: Session,
) -> User:
    """
    保存用户的模型配置
    """
    provider = provider.strip()
    base_url = base_url.strip()
    model = model.strip()
    api_key = api_key.strip()

    if not provider or not model:
        raise ValueError("模型提供方和模型名不能为空")

    # 校验 Base URL
    if not validate_base_url(base_url):
        raise ValueError("无效的 Base URL，请检查地址格式")

    # 校验 API Key 不能为空
    if not api_key:
        raise ValueError("API Key 不能为空")

    user.llm_provider = provider
    user.llm_base_url = base_url
    user.llm_model = model
    user.encrypted_api_key = encrypt_value(api_key)

    db.commit()
    db.refresh(user)
    return user


def clear_model_config(user: User, db: Session) -> User:
    """
    清除用户的模型配置
    """
    user.llm_provider = None
    user.llm_base_url = None
    user.llm_model = None
    user.encrypted_api_key = None

    db.commit()
    db.refresh(user)
    return user


def test_model_connection(base_url: str, api_key: str, model: str) -> dict:
    """
    测试模型连接
    调用 LLM 接口验证 API Key 是否有效
    """
    from openai import OpenAI

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=10.0,
        )
        # 简单测试：调用 models.list 或一个简单的 completion
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Hello, this is a connection test. Reply with 'OK'."}],
            max_tokens=5,
            temperature=0.0,
        )
        if response and response.choices:
            return {"success": True, "message": "连接测试成功"}
        return {"success": False, "error": "连接测试失败：未收到有效响应"}
    except Exception:
        return {"success": False, "error": "连接失败，请检查 Base URL、模型名和 API Key"}


def get_preset_providers() -> Dict[str, Dict[str, str]]:
    """
    获取预设模型列表
    """
    return PRESET_PROVIDERS
