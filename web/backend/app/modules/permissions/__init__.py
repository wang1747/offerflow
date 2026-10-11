from fastapi import Depends, HTTPException, status

from app.modules.auth.routes import get_current_user_required
from app.models.user import User

ROLE_ADMIN = "admin"
ROLE_USER = "user"


def require_role(*roles: str):
    def dependency(current_user: User = Depends(get_current_user_required)):
        role = current_user.role or ROLE_USER
        if role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user

    return dependency


def ensure_owner(user: User, resource):
    if getattr(resource, "user_id", None) != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该资源",
        )
    return resource
