from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from remote_control.storage.db import Database
from remote_control.storage.models import EventRecord, JobRecord


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
