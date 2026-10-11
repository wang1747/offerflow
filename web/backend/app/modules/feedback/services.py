"""项目建议反馈服务：提交、公开列表、点赞、管理员状态/回复。"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.feedback import Feedback, FeedbackLike
from app.models.user import User

_FEEDBACK_CATEGORIES = ["功能建议", "问题反馈", "体验优化", "其他"]
_FEEDBACK_STATUSES = ["pending", "accepted", "declined", "replied"]


def create_feedback(db: Session, user_id: str, category: str,
                    title: str, content: str) -> Feedback:
    if category not in _FEEDBACK_CATEGORIES:
        category = "其他"
    fb = Feedback(user_id=user_id, category=category, title=title.strip(), content=content.strip())
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb


def _feedback_out(fb: Feedback, author: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": fb.id,
        "user_id": fb.user_id,
        "author": author,
        "category": fb.category,
        "title": fb.title,
        "content": fb.content,
        "status": fb.status,
        "admin_reply": fb.admin_reply,
        "like_count": fb.like_count or 0,
        "created_at": str(fb.created_at) if fb.created_at else None,
        "updated_at": str(fb.updated_at) if fb.updated_at else None,
    }


def _fill_authors(db: Session, items: List[Dict[str, Any]], key="user_id") -> None:
    uids = {it[key] for it in items if it.get(key)}
    names = {u.id: u.name for u in db.query(User).filter(User.id.in_(uids)).all()}
    for it in items:
        it["author"] = names.get(it.get(key), "匿名用户")


def list_feedbacks(db: Session, category: Optional[str] = None,
                   status: Optional[str] = None,
                   page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    q = db.query(Feedback)
    if category:
        q = q.filter(Feedback.category == category)
    if status:
        q = q.filter(Feedback.status == status)
    total = q.count()
    rows = (q.order_by(Feedback.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    data = [_feedback_out(fb) for fb in rows]
    _fill_authors(db, data)
    return {"items": data, "total": total, "page": page, "page_size": page_size}


def list_my_feedbacks(db: Session, user_id: str) -> List[Dict[str, Any]]:
    rows = (db.query(Feedback).filter(Feedback.user_id == user_id)
            .order_by(Feedback.created_at.desc()).all())
    data = [_feedback_out(fb) for fb in rows]
    _fill_authors(db, data)
    return data


def toggle_like(db: Session, feedback_id: str, user_id: str) -> Dict[str, Any]:
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        raise ValueError("建议不存在")
    existing = (db.query(FeedbackLike)
                .filter(FeedbackLike.feedback_id == feedback_id, FeedbackLike.user_id == user_id)
                .first())
    if existing:
        db.delete(existing)
        fb.like_count = max(0, (fb.like_count or 0) - 1)
        liked = False
    else:
        db.add(FeedbackLike(feedback_id=feedback_id, user_id=user_id))
        fb.like_count = (fb.like_count or 0) + 1
        liked = True
    db.commit()
    return {"liked": liked, "like_count": fb.like_count or 0}


def admin_update(db: Session, feedback_id: str, status: Optional[str],
                 admin_reply: Optional[str]) -> Optional[Feedback]:
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        return None
    if status and status in _FEEDBACK_STATUSES:
        fb.status = status
    if admin_reply is not None:
        fb.admin_reply = admin_reply.strip()
    db.commit()
    db.refresh(fb)
    return fb
