from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from remote_control.storage.db import Database
from remote_control.storage.models import (
    ApprovalRecord,
    EventRecord,
    ExecutionLeaseRecord,
    HostRecord,
    JobRecord,
    ProjectSessionRecord,
    ProjectWorkRecord,
    RecoveryRecord,
    RemoteExecutionRecord,
    SessionRecord,
    TelegramMessageBindingRecord,
    TelegramProjectTopicRecord,
)

logger = logging.getLogger(__name__)


class JobRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, job: JobRecord) -> JobRecord:
        async with self.db.sessions() as session:
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    async def add_with_event(
        self,
        job: JobRecord,
        *,
        event_type: str,
        host_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> JobRecord:
        record = EventRecord(
            event_type=event_type,
            job_id=job.id,
            project_id=job.project_id,
            host_id=host_id,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
        )
        async with self.db.sessions() as session:
            session.add(job)
            session.add(record)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
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

    async def list_states(self, states: set[str]) -> list[JobRecord]:
        if not states:
            return []
        async with self.db.sessions() as session:
            result = await session.execute(
                select(JobRecord)
                .where(JobRecord.state.in_(states))
                .order_by(JobRecord.created_at)
            )
            return list(result.scalars())

    async def list_active_for_user(
        self,
        user_id: str,
        *,
        project_id: str | None = None,
    ) -> list[JobRecord]:
        terminal = ("FAILED", "CANCELLED", "COMPLETED")
        async with self.db.sessions() as session:
            query = (
                select(JobRecord)
                .where(JobRecord.requested_by_user == user_id)
                .where(JobRecord.state.not_in(terminal))
            )
            if project_id is not None:
                query = query.where(JobRecord.project_id == project_id)
            result = await session.execute(query.order_by(JobRecord.created_at.desc()))
            return list(result.scalars())

    async def latest_for_user_project(
        self,
        user_id: str,
        project_id: str,
    ) -> JobRecord | None:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(JobRecord)
                .where(JobRecord.requested_by_user == user_id)
                .where(JobRecord.project_id == project_id)
                .order_by(JobRecord.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

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

    async def transition_with_event(
        self,
        job_id: str,
        *,
        expected_state: str,
        target_state: str,
        changes: dict[str, Any] | None = None,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> JobRecord:
        async with self.db.sessions() as session:
            job = await session.get(JobRecord, job_id)
            if job is None:
                raise KeyError(f"unknown job: {job_id}")
            if job.state != expected_state:
                raise RuntimeError(
                    "job state changed during lifecycle transition: "
                    f"job={job.id} expected={expected_state} actual={job.state}"
                )

            job.state = target_state
            for key, value in (changes or {}).items():
                setattr(job, key, value)

            session.add(
                EventRecord(
                    event_type=event_type,
                    job_id=job.id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload_json=json.dumps(payload or {}, ensure_ascii=False),
                )
            )
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
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
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        delays = (0.05, 0.1)
        for attempt in range(1, len(delays) + 2):
            record = EventRecord(
                event_type=event_type,
                job_id=job_id,
                project_id=project_id,
                host_id=host_id,
                payload_json=payload_json,
            )
            try:
                async with self.db.sessions() as session:
                    session.add(record)
                    await session.commit()
                    await session.refresh(record)
                    return record
            except OperationalError as exc:
                if not _is_sqlite_busy_error(exc) or attempt > len(delays):
                    raise
                delay = delays[attempt - 1]
                logger.warning(
                    "SQLite event write busy; retrying event_type=%s attempt=%s delay=%.2fs",
                    event_type,
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable event append retry state")


def _is_sqlite_busy_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


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
                if record.runner_instance_id is not None:
                    current.runner_instance_id = record.runner_instance_id
                if record.runner_boot_id is not None:
                    current.runner_boot_id = record.runner_boot_id
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


class ExecutionLeaseRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, record: ExecutionLeaseRecord) -> ExecutionLeaseRecord:
        async with self.db.sessions() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(record)
            return record

    async def assign_job(
        self,
        *,
        record: ExecutionLeaseRecord,
        expected_state: str,
        assigned_host: str,
    ) -> tuple[JobRecord, ExecutionLeaseRecord]:
        async with self.db.sessions() as session:
            job = await session.get(JobRecord, record.job_id)
            if job is None:
                raise KeyError(f"unknown job: {record.job_id}")
            if job.state != expected_state:
                raise RuntimeError(
                    f"job state changed during assignment: "
                    f"job={job.id} expected={expected_state} actual={job.state}"
                )

            result = await session.execute(
                select(ExecutionLeaseRecord)
                .where(ExecutionLeaseRecord.job_id == record.job_id)
                .limit(1)
            )
            lease = result.scalar_one_or_none()
            if lease is None:
                session.add(record)
                lease = record
            elif lease.lease_key != record.lease_key:
                raise ValueError(
                    "job already owns a different execution lease: "
                    f"job={record.job_id} current={lease.lease_key} "
                    f"requested={record.lease_key}"
                )

            job.assigned_host = assigned_host
            job.state = "ASSIGNED"
            session.add(
                EventRecord(
                    event_type="JOB_ASSIGNED",
                    job_id=job.id,
                    project_id=job.project_id,
                    host_id=assigned_host,
                    payload_json=json.dumps(
                        {
                            "from": expected_state,
                            "to": "ASSIGNED",
                            "atomic_with_execution_lease": True,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(job)
            await session.refresh(lease)
            return job, lease

    async def get(self, lease_key: str) -> ExecutionLeaseRecord | None:
        async with self.db.sessions() as session:
            return await session.get(ExecutionLeaseRecord, lease_key)

    async def get_for_job(self, job_id: str) -> ExecutionLeaseRecord | None:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ExecutionLeaseRecord)
                .where(ExecutionLeaseRecord.job_id == job_id)
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def list(self) -> list[ExecutionLeaseRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ExecutionLeaseRecord)
                .order_by(ExecutionLeaseRecord.acquired_at)
            )
            return list(result.scalars())

    async def delete_for_job(self, job_id: str) -> None:
        async with self.db.sessions() as session:
            record = await session.execute(
                select(ExecutionLeaseRecord)
                .where(ExecutionLeaseRecord.job_id == job_id)
                .limit(1)
            )
            target = record.scalar_one_or_none()
            if target is None:
                return
            await session.delete(target)
            await session.commit()


class RemoteExecutionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, execution_id: str) -> RemoteExecutionRecord | None:
        async with self.db.sessions() as session:
            return await session.get(RemoteExecutionRecord, execution_id)

    async def list_for_host(
        self,
        host_id: str,
        *,
        include_acknowledged: bool = False,
    ) -> list[RemoteExecutionRecord]:
        async with self.db.sessions() as session:
            query = select(RemoteExecutionRecord).where(
                RemoteExecutionRecord.host_id == host_id
            )
            if not include_acknowledged:
                query = query.where(RemoteExecutionRecord.state != "ACKNOWLEDGED")
            result = await session.execute(
                query.order_by(RemoteExecutionRecord.started_at)
            )
            return list(result.scalars())

    async def list_for_job(self, job_id: str) -> list[RemoteExecutionRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(RemoteExecutionRecord)
                .where(RemoteExecutionRecord.job_id == job_id)
                .order_by(RemoteExecutionRecord.started_at)
            )
            return list(result.scalars())

    async def upsert(
        self,
        execution_id: str,
        **changes: Any,
    ) -> RemoteExecutionRecord:
        async with self.db.sessions() as session:
            record = await session.get(RemoteExecutionRecord, execution_id)
            if record is None:
                record = RemoteExecutionRecord(
                    execution_id=execution_id,
                    **changes,
                )
                session.add(record)
            else:
                for key, value in changes.items():
                    setattr(record, key, value)
            await session.commit()
            await session.refresh(record)
            return record

    async def mark_result_received(
        self,
        execution_id: str,
        *,
        session_id: str | None,
    ) -> RemoteExecutionRecord | None:
        async with self.db.sessions() as session:
            record = await session.get(RemoteExecutionRecord, execution_id)
            if record is None:
                return None
            record.state = "RESULT_RECEIVED"
            if session_id:
                record.session_id = session_id
            record.result_received_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(record)
            return record

    async def mark_acknowledged(
        self,
        execution_id: str,
    ) -> RemoteExecutionRecord | None:
        async with self.db.sessions() as session:
            record = await session.get(RemoteExecutionRecord, execution_id)
            if record is None:
                return None
            record.state = "ACKNOWLEDGED"
            record.acknowledged_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(record)
            return record


class ProjectSessionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, record: ProjectSessionRecord) -> ProjectSessionRecord:
        async with self.db.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get(self, session_id: str) -> ProjectSessionRecord | None:
        async with self.db.sessions() as session:
            return await session.get(ProjectSessionRecord, session_id)

    async def active_for(
        self,
        project_id: str,
        owner_user_id: str,
    ) -> ProjectSessionRecord | None:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ProjectSessionRecord)
                .where(ProjectSessionRecord.project_id == project_id)
                .where(ProjectSessionRecord.owner_user_id == owner_user_id)
                .where(ProjectSessionRecord.status.in_(("IDLE", "ACTIVE")))
                .where(ProjectSessionRecord.closed_at.is_(None))
                .order_by(ProjectSessionRecord.last_active_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def list_for_user(
        self,
        owner_user_id: str,
        *,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[ProjectSessionRecord]:
        async with self.db.sessions() as session:
            query = select(ProjectSessionRecord).where(
                ProjectSessionRecord.owner_user_id == owner_user_id
            )
            if project_id is not None:
                query = query.where(ProjectSessionRecord.project_id == project_id)
            result = await session.execute(
                query.order_by(ProjectSessionRecord.last_active_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def list(self, limit: int = 100) -> list[ProjectSessionRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ProjectSessionRecord)
                .order_by(ProjectSessionRecord.last_active_at.desc())
                .limit(limit)
            )
            return list(result.scalars())

    async def list_locked(self, limit: int = 1000) -> list[ProjectSessionRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ProjectSessionRecord)
                .where(ProjectSessionRecord.locked_by_job_id.is_not(None))
                .order_by(ProjectSessionRecord.last_active_at)
                .limit(limit)
            )
            return list(result.scalars())

    async def update(self, session_id: str, **changes: Any) -> ProjectSessionRecord:
        async with self.db.sessions() as session:
            record = await session.get(ProjectSessionRecord, session_id)
            if record is None:
                raise KeyError(f"unknown project session: {session_id}")
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

    async def expired_pending(self, now: datetime) -> list[ApprovalRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.status == "PENDING")
                .where(ApprovalRecord.expires_at.is_not(None))
                .where(ApprovalRecord.expires_at <= now)
                .order_by(ApprovalRecord.expires_at)
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


class RecoveryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, job_id: str) -> RecoveryRecord | None:
        async with self.db.sessions() as session:
            return await session.get(RecoveryRecord, job_id)

    async def list(self) -> list[RecoveryRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(RecoveryRecord).order_by(RecoveryRecord.updated_at)
            )
            return list(result.scalars())

    async def upsert(self, job_id: str, **changes: Any) -> RecoveryRecord:
        async with self.db.sessions() as session:
            record = await session.get(RecoveryRecord, job_id)
            if record is None:
                record = RecoveryRecord(job_id=job_id, **changes)
                session.add(record)
            else:
                for key, value in changes.items():
                    setattr(record, key, value)
            await session.commit()
            await session.refresh(record)
            return record

    async def delete(self, job_id: str) -> None:
        async with self.db.sessions() as session:
            record = await session.get(RecoveryRecord, job_id)
            if record is None:
                return
            await session.delete(record)
            await session.commit()


class ProjectWorkRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, record: ProjectWorkRecord) -> ProjectWorkRecord:
        async with self.db.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get(self, job_id: str) -> ProjectWorkRecord | None:
        async with self.db.sessions() as session:
            return await session.get(ProjectWorkRecord, job_id)

    async def list(self, limit: int = 100) -> list[ProjectWorkRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(ProjectWorkRecord)
                .order_by(ProjectWorkRecord.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars())

    async def update(self, job_id: str, **changes: Any) -> ProjectWorkRecord:
        async with self.db.sessions() as session:
            record = await session.get(ProjectWorkRecord, job_id)
            if record is None:
                raise KeyError(f"unknown project work: {job_id}")
            for key, value in changes.items():
                setattr(record, key, value)
            await session.commit()
            await session.refresh(record)
            return record


class TelegramProjectTopicRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(
        self,
        *,
        user_id: str,
        project_id: str,
    ) -> TelegramProjectTopicRecord | None:
        async with self.db.sessions() as session:
            return await session.get(
                TelegramProjectTopicRecord,
                {"user_id": user_id, "project_id": project_id},
            )

    async def find_by_thread(
        self,
        *,
        chat_id: str,
        message_thread_id: int,
    ) -> TelegramProjectTopicRecord | None:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(TelegramProjectTopicRecord)
                .where(TelegramProjectTopicRecord.chat_id == chat_id)
                .where(TelegramProjectTopicRecord.message_thread_id == message_thread_id)
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def list_for_user(self, user_id: str) -> list[TelegramProjectTopicRecord]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(TelegramProjectTopicRecord)
                .where(TelegramProjectTopicRecord.user_id == user_id)
                .order_by(TelegramProjectTopicRecord.project_id)
            )
            return list(result.scalars())

    async def upsert(
        self,
        *,
        user_id: str,
        project_id: str,
        chat_id: str,
        message_thread_id: int,
        topic_name: str,
    ) -> TelegramProjectTopicRecord:
        async with self.db.sessions() as session:
            record = await session.get(
                TelegramProjectTopicRecord,
                {"user_id": user_id, "project_id": project_id},
            )
            if record is None:
                record = TelegramProjectTopicRecord(
                    user_id=user_id,
                    project_id=project_id,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    topic_name=topic_name,
                )
                session.add(record)
            else:
                record.chat_id = chat_id
                record.message_thread_id = message_thread_id
                record.topic_name = topic_name
            await session.commit()
            await session.refresh(record)
            return record


class TelegramMessageBindingRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(
        self,
        *,
        chat_id: str,
        message_id: int,
    ) -> TelegramMessageBindingRecord | None:
        async with self.db.sessions() as session:
            return await session.get(
                TelegramMessageBindingRecord,
                {"chat_id": chat_id, "message_id": message_id},
            )

    async def upsert(
        self,
        *,
        chat_id: str,
        message_id: int,
        message_thread_id: int | None,
        user_id: str,
        project_id: str,
        job_id: str,
    ) -> TelegramMessageBindingRecord:
        async with self.db.sessions() as session:
            record = await session.get(
                TelegramMessageBindingRecord,
                {"chat_id": chat_id, "message_id": message_id},
            )
            if record is None:
                record = TelegramMessageBindingRecord(
                    chat_id=chat_id,
                    message_id=message_id,
                    message_thread_id=message_thread_id,
                    user_id=user_id,
                    project_id=project_id,
                    job_id=job_id,
                )
                session.add(record)
            else:
                record.message_thread_id = message_thread_id
                record.user_id = user_id
                record.project_id = project_id
                record.job_id = job_id
            await session.commit()
            await session.refresh(record)
            return record
