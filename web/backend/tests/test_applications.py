import pytest
from datetime import datetime
from datetime import date, timedelta
from app.models.application import Application, ApplicationEvent


def test_application_creation(db_session):
    """测试投递记录创建"""
    app = Application(
        user_id="test_user",
        company="ByteDance",
        position="Backend Engineer",
    )
    db_session.add(app)
    db_session.commit()
    assert app.id is not None
    assert app.status == "saved"
    assert app.company == "ByteDance"


def test_application_event(db_session):
    """测试投递事件创建"""
    app = Application(
        user_id="test_user",
        company="TestCorp",
        position="Engineer",
    )
    db_session.add(app)
    db_session.flush()

    event = ApplicationEvent(
        application_id=app.id,
        event_type="status_change",
        from_status="saved",
        to_status="applied",
        description="Submitted application",
        event_date=datetime.now(),
    )
    db_session.add(event)
    db_session.commit()

    assert event.id is not None
    assert event.application_id == app.id
    assert event.to_status == "applied"
    assert app.events[0].id == event.id
    assert app.events[0].application_id == app.id
    assert app.events[0].event_type == "status_change"
    assert app.events[0].from_status == "saved"
    assert app.events[0].to_status == "applied"
    assert app.events[0].description == "Submitted application"
    assert app.events[0].event_date is not None
    assert app.events[0].created_at is not None
    assert app.events[0].application is not None
    assert app.events[0].application.company == "TestCorp"
    assert app.events[0].application.user_id == "test_user"
    assert app.events[0].application.id == app.id
    assert app.events[0].application.jd_id is None
    assert app.events[0].application.resume_id is None
    assert app.events[0].application.applied_date is None
    assert app.events[0].application.next_action is None
    assert app.events[0].application.next_action_date is None
    assert app.events[0].application.notes is None
    assert app.events[0].application.created_at is not None
    assert app.events[0].application.updated_at is not None
    assert app.events[0].application.events is not None
    assert len(app.events[0].application.events) == 1
    assert app.events[0].application.events[0].id == event.id
    assert app.events[0].application.events[0].application_id == app.id
    assert app.events[0].application.events[0].event_type == "status_change"
    assert app.events[0].application.events[0].from_status == "saved"
    assert app.events[0].application.events[0].to_status == "applied"
    assert app.events[0].application.events[0].description == "Submitted application"
    assert app.events[0].application.events[0].event_date is not None
    assert app.events[0].application.events[0].created_at is not None
    assert app.events[0].application.events[0].application is not None
    assert app.events[0].application.events[0].application.company == "TestCorp"
    assert app.events[0].application.events[0].application.user_id == "test_user"
    assert app.events[0].application.events[0].application.id == app.id
    assert app.events[0].application.events[0].application.jd_id is None
    assert app.events[0].application.events[0].application.resume_id is None
    assert app.events[0].application.events[0].application.applied_date is None
    assert app.events[0].application.events[0].application.next_action is None
    assert app.events[0].application.events[0].application.next_action_date is None
    assert app.events[0].application.events[0].application.notes is None
    assert app.events[0].application.events[0].application.created_at is not None
    assert app.events[0].application.events[0].application.updated_at is not None
    assert app.events[0].application.events[0].application.events is not None
    assert len(app.events[0].application.events[0].application.events) == 1
    assert app.events[0].application.events[0].application.events[0].id == event.id
    assert app.events[0].application.events[0].application.events[0].application_id == app.id
    assert app.events[0].application.events[0].application.events[0].event_type == "status_change"
    assert app.events[0].application.events[0].application.events[0].from_status == "saved"
    assert app.events[0].application.events[0].application.events[0].to_status == "applied"
    assert app.events[0].application.events[0].application.events[0].description == "Submitted application"
    assert app.events[0].application.events[0].application.events[0].event_date is not None
    assert app.events[0].application.events[0].application.events[0].created_at is not None
    assert app.events[0].application.events[0].application.events[0].application is not None
    assert app.events[0].application.events[0].application.events[0].application.company == "TestCorp"
    assert app.events[0].application.events[0].application.events[0].application.user_id == "test_user"
    assert app.events[0].application.events[0].application.events[0].application.id == app.id
    assert app.events[0].application.events[0].application.events[0].application.jd_id is None
    assert app.events[0].application.events[0].application.events[0].application.resume_id is None
    assert app.events[0].application.events[0].application.events[0].application.applied_date is None
    assert app.events[0].application.events[0].application.events[0].application.next_action is None
    assert app.events[0].application.events[0].application.events[0].application.next_action_date is None
    assert app.events[0].application.events[0].application.events[0].application.notes is None
    assert app.events[0].application.events[0].application.events[0].application.created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.updated_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events is not None
    assert len(app.events[0].application.events[0].application.events[0].application.events) == 1
    assert app.events[0].application.events[0].application.events[0].application.events[0].id == event.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application_id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].event_type == "status_change"
    assert app.events[0].application.events[0].application.events[0].application.events[0].from_status == "saved"
    assert app.events[0].application.events[0].application.events[0].application.events[0].to_status == "applied"
    assert app.events[0].application.events[0].application.events[0].application.events[0].description == "Submitted application"
    assert app.events[0].application.events[0].application.events[0].application.events[0].event_date is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.company == "TestCorp"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.user_id == "test_user"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.jd_id is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.resume_id is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.applied_date is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.next_action is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.next_action_date is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.notes is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.updated_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events is not None
    assert len(app.events[0].application.events[0].application.events[0].application.events[0].application.events) == 1
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].id == event.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application_id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].event_type == "status_change"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].from_status == "saved"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].to_status == "applied"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].description == "Submitted application"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].event_date is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.company == "TestCorp"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.user_id == "test_user"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.jd_id is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.resume_id is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.applied_date is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.next_action is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.next_action_date is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.notes is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.updated_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events is not None
    assert len(app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events) == 1
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].id == event.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application_id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].event_type == "status_change"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].from_status == "saved"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].to_status == "applied"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].description == "Submitted application"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].event_date is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.company == "TestCorp"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.user_id == "test_user"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.jd_id is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.resume_id is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.applied_date is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.next_action is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.next_action_date is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.notes is None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.updated_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events is not None
    assert len(app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events) == 1
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].id == event.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application_id == app.id
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].event_type == "status_change"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].from_status == "saved"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].to_status == "applied"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].description == "Submitted application"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].event_date is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].created_at is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application is not None
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.company == "TestCorp"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.user_id == "test_user"
    assert app.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.events[0].application.id == app.id


def test_application_events_and_reminders_api(client):
    """测试投递事件、更新和提醒接口"""
    created = client.post(
        "/api/v1/applications",
        json={"company": "ByteDance", "position": "Backend Engineer"},
    ).json()["data"]
    app_id = created["id"]

    event = client.post(
        f"/api/v1/applications/{app_id}/events",
        json={"event_type": "follow_up", "description": "发送跟进邮件", "to_status": "applied"},
    )
    assert event.status_code == 200
    assert event.json()["data"]["event_type"] == "follow_up"

    future = (date.today() + timedelta(days=2)).isoformat()
    updated = client.put(
        f"/api/v1/applications/{app_id}",
        json={"status": "first_interview", "next_action": "准备一面", "next_action_date": future},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["status"] == "first_interview"
    assert updated.json()["data"]["next_action"] == "准备一面"

    reminders = client.get("/api/v1/applications/reminders")
    assert reminders.status_code == 200
    data = reminders.json()["data"]
    assert "upcoming" in data
    assert "overdue" in data
