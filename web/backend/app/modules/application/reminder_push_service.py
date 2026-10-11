import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.models.user import User
from app.modules.application.reminder_service import get_company_interview_articles, get_reminders

logger = logging.getLogger(__name__)


async def push_reminders_for_user(
    webhook_url: str,
    db: Session,
    user: User,
) -> int | None:
    reminders = get_reminders(db, user.id)
    if not reminders.get("has_reminders"):
        return None

    for section in ("overdue", "upcoming"):
        for item in reminders.get(section, []):
            item["interview_articles"] = get_company_interview_articles(
                item["company"],
                db,
                user.id,
            )

    payload = {
        "user": {"id": user.id, "name": user.name},
        "reminders": reminders,
        "sent_at": datetime.now().isoformat(),
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(webhook_url, json=payload)
        response.raise_for_status()

    logger.info("提醒推送成功: user=%s", user.id)
    return response.status_code
