import uuid
from typing import Optional, List
from datetime import date, datetime
from sqlalchemy.orm import Session, selectinload

from app.models.application import Application, ApplicationEvent
from app.models.jd import JobDescription
from app.models.resume import Resume


APPLICATION_STATUSES = [
    "saved",
    "applied",
    "online_test",
    "first_interview",
    "second_interview",
    "hr_round",
    "offered",
    "accepted",
    "rejected",
]


def create_application(
    company: str,
    position: str,
    db: Session,
    user_id: str,
    jd_id: Optional[str] = None,
    resume_id: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Application:
    """创建投递记录。

    client_id：客户端（桌面版离线）自生成的 id。同用户同 id 已存在时**直接返回已有记录**
    （幂等）—— 离线队列重复上行不会建出重复数据，这是双向同步的前提。
    """
    if client_id:
        existing = db.query(Application).filter(
            Application.id == client_id,
            Application.user_id == user_id,
        ).first()
        if existing:
            return existing
    if jd_id:
        jd = db.query(JobDescription).filter(
            JobDescription.id == jd_id,
            JobDescription.user_id == user_id
        ).first()
        if not jd:
            raise ValueError("JD不存在")
    if resume_id:
        resume = db.query(Resume).filter(
            Resume.id == resume_id,
            Resume.user_id == user_id
        ).first()
        if not resume:
            raise ValueError("简历不存在")
    app = Application(
        id=client_id or str(uuid.uuid4()),
        user_id=user_id,
        company=company,
        position=position,
        jd_id=jd_id,
        resume_id=resume_id,
        status="saved",
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def list_applications(db: Session, user_id: str, status: Optional[str] = None) -> list:
    """获取投递列表（支持按状态筛选）"""
    query = db.query(Application).filter(
        Application.user_id == user_id
    )
    if status:
        query = query.filter(Application.status == status)
    return query.order_by(Application.updated_at.desc()).all()


def get_application(app_id: str, db: Session, user_id: str) -> Optional[Application]:
    """获取单个投递记录"""
    return db.query(Application).filter(
        Application.id == app_id,
        Application.user_id == user_id
    ).first()


def update_application_status(
    app_id: str,
    new_status: str,
    db: Session,
    user_id: str
) -> Optional[Application]:
    """更新投递状态"""
    app = get_application(app_id, db, user_id)
    if not app:
        return None
    if new_status not in APPLICATION_STATUSES:
        raise ValueError(f"无效状态: {new_status}")
    old_status = app.status
    event = ApplicationEvent(
        application_id=app_id,
        event_type="status_change",
        from_status=old_status,
        to_status=new_status,
        description=f"状态变更: {old_status} → {new_status}",
        event_date=datetime.now(),
    )
    app.status = new_status
    db.add(event)
    db.commit()
    db.refresh(app)
    return app


def update_application(
    app_id: str,
    db: Session,
    user_id: str,
    status: Optional[str] = None,
    applied_date: Optional[date] = None,
    next_action: Optional[str] = None,
    next_action_date: Optional[date] = None,
    notes: Optional[str] = None,
) -> Optional[Application]:
    """更新投递记录的可编辑字段，状态变更时写入事件"""
    app = get_application(app_id, db, user_id)
    if not app:
        return None
    if status is not None:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"无效状态: {status}")
        if status != app.status:
            event = ApplicationEvent(
                application_id=app_id,
                event_type="status_change",
                from_status=app.status,
                to_status=status,
                description=f"状态变更: {app.status} → {status}",
                event_date=datetime.now(),
            )
            db.add(event)
            app.status = status
    if applied_date is not None:
        app.applied_date = applied_date
    if next_action is not None:
        app.next_action = next_action
    if next_action_date is not None:
        app.next_action_date = next_action_date
    if notes is not None:
        app.notes = notes
    db.commit()
    db.refresh(app)
    return app


def delete_application(app_id: str, db: Session, user_id: str) -> bool:
    """删除投递记录"""
    app = get_application(app_id, db, user_id)
    if not app:
        return False
    db.delete(app)
    db.commit()
    return True


def add_event(
    application_id: str,
    event_type: str,
    db: Session,
    user_id: str,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[ApplicationEvent]:
    """添加投递事件"""
    app = db.query(Application).filter(
        Application.id == application_id,
        Application.user_id == user_id
    ).first()
    if not app:
        return None
    event = ApplicationEvent(
        application_id=application_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        description=description,
        event_date=datetime.now(),
    )
    if to_status:
        app.status = to_status
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_statistics(db: Session, user_id: str) -> dict:
    """获取投递统计数据"""
    apps = db.query(Application).options(
        selectinload(Application.events)
    ).filter(
        Application.user_id == user_id
    ).all()
    
    total = len(apps)
    status_counts = {status: 0 for status in APPLICATION_STATUSES}
    for app in apps:
        status_counts[app.status] = status_counts.get(app.status, 0) + 1
    
    return {
        "total": total,
        "status_counts": status_counts,
        "status_order": APPLICATION_STATUSES,
        "conversion_rate": {
            "applied_to_interview": _calc_conversion(apps, "applied", "first_interview"),
            "interview_to_offer": _calc_conversion(apps, "first_interview", "offered")
        }
    }


def _calc_conversion(apps: list, from_status: str, to_status: str) -> float:
    """计算转化率"""
    from_count = sum(1 for a in apps if _reached_status(a, from_status))
    to_count = sum(1 for a in apps if _reached_status(a, to_status))
    if from_count == 0:
        return 0.0
    return round(to_count / from_count * 100, 1)


def _reached_status(app: Application, status: str) -> bool:
    """检查是否到达过某个状态（优先使用事件历史）"""
    if app.events:
        return any(event.to_status == status for event in app.events)
    current_idx = APPLICATION_STATUSES.index(app.status) if app.status in APPLICATION_STATUSES else -1
    target_idx = APPLICATION_STATUSES.index(status) if status in APPLICATION_STATUSES else -1
    return current_idx >= target_idx