from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from remote_control.storage.db import Database
from remote_control.storage.models import (
    ApprovalRecord,
    EventRecord,
    HostRecord,
    JobRecord,
    SessionRecord,
)


class JobRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, job: JobRecord) -> JobRecord:
        async with self.db.sessions() as session:
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    async def get(self, job_id: str) -> JobRecord | None:
        async with self.db.sessions() as session:
            return await session.get(JobRecord, job_id)

    async def list(self, limit: int = 50) -> list[JobRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def list_active_for_user(self, user_id: str) -> list[JobRecord]:
        terminal = ("FAILED", "CANCELLED", "COMPLETED")
        async with self.db.sessions() as session:
            result = await session.execute(
                select(JobRecord)
                .where(JobRecord.requested_by_user == user_id)
                .where(JobRecord.state.not_in(terminal))
                .order_by(JobRecord.created_at.desc())
            )
            return list(result.scalars())

    async def update(self, job_id: str, **changes: Any) -> JobRecord:
        async with self.db.sessions() as session:
            job = await session.get(JobRecord, job_id)
            if job is None:
                raise KeyError(f"unknown job: {job_id}")
            for key, value in changes.items():
                setattr(job, key, value)
            await session.commit()
            await session.refresh(job)
            return job


class EventRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self,
        event_type: str,
        *,
        job_id: str | None = None,
        project_id: str | None = None,
        host_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        record = EventRecord(
            event_type=event_type,
            job_id=job_id,
            project_id=project_id,
            host_id=host_id,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
        )
        async with self.db.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record


class HostRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, host_id: str) -> HostRecord | None:
        async with self.db.sessions() as session:
            return await session.get(HostRecord, host_id)

    async def list(self) -> list[HostRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(select(HostRecord).order_by(HostRecord.id))
            return list(result.scalars())

    async def upsert(self, record: HostRecord) -> HostRecord:
        async with self.db.sessions() as session:
            current = await session.get(HostRecord, record.id)
            if current is None:
                session.add(record)
                target = record
            else:
                current.name = record.name
                current.os = record.os
                current.status = record.status
                current.capabilities_json = record.capabilities_json
                current.last_heartbeat = record.last_heartbeat
                target = current
            await session.commit()
            await session.refresh(target)
            return target

    async def update(self, host_id: str, **changes: Any) -> HostRecord:
        async with self.db.sessions() as session:
            record = await session.get(HostRecord, host_id)
            if record is None:
                raise KeyError(f"unknown host: {host_id}")
            for key, value in changes.items():
                setattr(record, key, value)
            await session.commit()
            await session.refresh(record)
            return record


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, record: SessionRecord) -> SessionRecord:
        async with self.db.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self.db.sessions() as session:
            return await session.get(SessionRecord, session_id)

    async def get_for_job(self, job_id: str) -> SessionRecord | None:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(SessionRecord).where(SessionRecord.job_id == job_id)
            )
            return result.scalar_one_or_none()

    async def list(self, limit: int = 100) -> list[SessionRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(SessionRecord)
                .order_by(SessionRecord.last_active_at.desc())
                .limit(limit)
            )
            return list(result.scalars())

    async def update(self, session_id: str, **changes: Any) -> SessionRecord:
        async with self.db.sessions() as session:
            record = await session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(f"unknown session: {session_id}")
            for key, value in changes.items():
                setattr(record, key, value)
            await session.commit()
            await session.refresh(record)
            return record


class ApprovalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, record: ApprovalRecord) -> ApprovalRecord:
        async with self.db.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        async with self.db.sessions() as session:
            return await session.get(ApprovalRecord, approval_id)

    async def list(self, limit: int = 100) -> list[ApprovalRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .order_by(ApprovalRecord.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars())

    async def pending_for_job(self, job_id: str) -> ApprovalRecord | None:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.job_id == job_id)
                .where(ApprovalRecord.status == "PENDING")
                .order_by(ApprovalRecord.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def pending_for_user(self, user_id: str) -> list[ApprovalRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.requested_by_user == user_id)
                .where(ApprovalRecord.status == "PENDING")
                .order_by(ApprovalRecord.created_at.desc())
            )
            return list(result.scalars())

    async def update(self, approval_id: str, **changes: Any) -> ApprovalRecord:
        async with self.db.sessions() as session:
            record = await session.get(ApprovalRecord, approval_id)
            if record is None:
                raise KeyError(f"unknown approval: {approval_id}")
            for key, value in changes.items():
                setattr(record, key, value)
            await session.commit()
            await session.refresh(record)
            return record
