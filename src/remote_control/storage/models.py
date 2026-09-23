from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    requested_by_channel: Mapped[str] = mapped_column(String(64))
    requested_by_user: Mapped[str] = mapped_column(String(128), index=True)
    requested_host: Mapped[str] = mapped_column(String(128), default="auto")
    assigned_host: Mapped[str | None] = mapped_column(String(128), nullable=True)
    instruction: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    external_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    host_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HostRecord(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    os: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_type: Mapped[str] = mapped_column(String(64), default="codex")
    external_session_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    requested_by_user: Mapped[str] = mapped_column(String(128), index=True)
    approval_type: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    selected_option: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
