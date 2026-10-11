import uuid

from sqlalchemy import Column, String, DateTime, Text, Date, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class Application(Base):
    __tablename__ = "applications"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    company = Column(String, nullable=False)
    position = Column(String, nullable=False)
    status = Column(String, nullable=False, default="saved")
    jd_id = Column(String, ForeignKey("job_descriptions.id"), nullable=True)
    resume_id = Column(String, ForeignKey("resumes.id"), nullable=True)
    applied_date = Column(Date, nullable=True)
    next_action = Column(Text, nullable=True)
    next_action_date = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ORM关联
    user = relationship("User")
    jd = relationship("JobDescription")
    resume = relationship("Resume")
    events = relationship("ApplicationEvent", cascade="all, delete-orphan")


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id = Column(String, ForeignKey("applications.id"), nullable=False)
    event_type = Column(String, nullable=False)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    event_date = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    # ORM关联
    application = relationship("Application", overlaps="events")
