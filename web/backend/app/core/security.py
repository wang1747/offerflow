import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Optional

import bcrypt

from app.config import get_settings


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
    settings = get_settings()
    now = int(time.time())
    expires_in = expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    exp = now + int(expires_in.total_seconds())

    header = {"alg": settings.jwt_algorithm, "typ": "JWT"}
    payload = {"sub": user_id, "iat": now, "exp": exp}

    header_segment = _b64url_encode(
        json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    payload_segment = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    secret = settings.jwt_secret_key.get_secret_value().encode("utf-8")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()

    return f"{header_segment}.{payload_segment}.{_b64url_encode(signature)}"


def decode_access_token(token: str) -> Optional[str]:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        settings = get_settings()
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        expected = hmac.new(
            settings.jwt_secret_key.get_secret_value().encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        actual = _b64url_decode(signature_segment)
        if not hmac.compare_digest(actual, expected):
            return None

        header = json.loads(_b64url_decode(header_segment).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
        if header.get("alg") != settings.jwt_algorithm:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload.get("sub")
    except Exception:
        return None


def get_token_user_id(token: str) -> Optional[str]:
    return decode_access_token(token)
