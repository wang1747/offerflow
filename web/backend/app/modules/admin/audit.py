"""审计日志写入：管理员写操作的统一留痕入口

用**独立 session** 写，保证审计失败不阻断（也不回滚）业务操作。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.core.database import SessionLocal
from app.models.audit import AdminAuditLog

logger = logging.getLogger(__name__)


def record_audit(
    operator,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    target_label: Optional[str] = None,
    detail: Optional[Any] = None,
    ip: Optional[str] = None,
) -> None:
    """
    写一条审计日志。

    :param operator: 执行操作的管理员 User 对象（或含 id/name 的任意对象）
    :param action: 动作码，如 update_role / toggle_active / reset_password / bind_email
    :param detail: dict 或 str，会序列化为 JSON 存库（用于记录 旧值 -> 新值）
    """
    try:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)

        db = SessionLocal()
        try:
            db.add(
                AdminAuditLog(
                    operator_id=str(getattr(operator, "id", "") or ""),
                    operator_name=getattr(operator, "name", None),
                    action=action,
                    target_type=target_type,
                    target_id=str(target_id) if target_id is not None else None,
                    target_label=target_label,
                    detail=detail,
                    ip=ip,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        # 审计失败绝不影响业务主流程，但必须留下痕迹
        logger.exception("审计日志写入失败: action=%s target=%s", action, target_id)
