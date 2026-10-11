import logging
from datetime import datetime, timedelta
from typing import List, Dict
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.interview import InterviewArticle

logger = logging.getLogger(__name__)


def get_overdue_applications(db: Session, user_id: str, days: int = 3) -> List[Dict]:
    """获取超期未跟进的投递记录"""
    cutoff = datetime.now() - timedelta(days=days)
    
    apps = db.query(Application).filter(
        Application.user_id == user_id,
        Application.status.in_(["applied", "online_test", "first_interview", "second_interview", "hr_round"]),
        Application.updated_at < cutoff
    ).order_by(Application.updated_at).all()
    
    return [
        {
            "id": app.id,
            "company": app.company,
            "position": app.position,
            "status": app.status,
            "updated_at": app.updated_at,
            "days_since_update": (datetime.now() - app.updated_at).days
        }
        for app in apps
    ]


def get_upcoming_interviews(db: Session, user_id: str, days: int = 3) -> List[Dict]:
    """获取即将到来的面试"""
    today = datetime.now().date()
    future = today + timedelta(days=days)
    
    apps = db.query(Application).filter(
        Application.user_id == user_id,
        Application.status.in_(["first_interview", "second_interview", "hr_round"]),
        Application.next_action_date.isnot(None),
        Application.next_action_date >= today,
        Application.next_action_date <= future
    ).order_by(Application.next_action_date).all()
    
    return [
        {
            "id": app.id,
            "company": app.company,
            "position": app.position,
            "status": app.status,
            "next_action": app.next_action,
            "next_action_date": app.next_action_date,
            "days_until": (app.next_action_date - today).days
        }
        for app in apps
    ]


def get_company_interview_articles(company: str, db: Session, user_id: str, limit: int = 5) -> List[Dict]:
    """获取某公司的面经"""
    articles = db.query(InterviewArticle).filter(
        InterviewArticle.user_id == user_id,
        InterviewArticle.company.ilike(f"%{company}%")
    ).order_by(InterviewArticle.created_at.desc()).limit(limit).all()
    
    return [
        {
            "id": art.id,
            "company": art.company,
            "position": art.position,
            "questions": art.questions or [],
            "difficulty": art.difficulty,
            "created_at": art.created_at
        }
        for art in articles
    ]


def get_reminders(db: Session, user_id: str) -> Dict:
    """获取所有提醒汇总"""
    overdue = get_overdue_applications(db, user_id)
    upcoming = get_upcoming_interviews(db, user_id)
    
    return {
        "overdue": overdue,
        "overdue_count": len(overdue),
        "upcoming": upcoming,
        "upcoming_count": len(upcoming),
        "has_reminders": len(overdue) > 0 or len(upcoming) > 0
    }
