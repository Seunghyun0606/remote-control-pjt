from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from remote_control.storage.models import SessionRecord
from remote_control.storage.repositories import EventRepository, SessionRepository


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    IDLE = "IDLE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _session_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"SESSION-{timestamp}-{uuid4().hex[:6].upper()}"


class SessionRegistry:
    def __init__(self, *, sessions: SessionRepository, events: EventRepository) -> None:
        self.sessions = sessions
        self.events = events

    async def record(
        self,
        *,
        job_id: str,
        project_id: str,
        host_id: str,
        external_session_id: str,
        status: SessionStatus = SessionStatus.ACTIVE,
    ) -> SessionRecord:
        now = datetime.now(timezone.utc)
        current = await self.sessions.get_for_job(job_id)
        if current is None:
            record = SessionRecord(
                id=_session_id(),
                job_id=job_id,
                project_id=project_id,
                host_id=host_id,
                agent_type="codex",
                external_session_id=external_session_id,
                status=status.value,
                last_active_at=now,
            )
            stored = await self.sessions.add(record)
            await self.events.append(
                "SESSION_STARTED",
                job_id=job_id,
                project_id=project_id,
                host_id=host_id,
                payload={
                    "session_id": stored.id,
                    "external_session_id": external_session_id,
                },
            )
            return stored

        previous_external = current.external_session_id
        updated = await self.sessions.update(
            current.id,
            host_id=host_id,
            external_session_id=external_session_id,
            status=status.value,
            last_active_at=now,
        )
        if previous_external != external_session_id:
            await self.events.append(
                "SESSION_REBOUND",
                job_id=job_id,
                project_id=project_id,
                host_id=host_id,
                payload={
                    "session_id": updated.id,
                    "previous_external_session_id": previous_external,
                    "external_session_id": external_session_id,
                },
            )
        return updated

    async def get_for_job(self, job_id: str) -> SessionRecord | None:
        return await self.sessions.get_for_job(job_id)

    async def list(self, limit: int = 100) -> list[SessionRecord]:
        return await self.sessions.list(limit=limit)

    async def mark(self, job_id: str, status: SessionStatus) -> SessionRecord | None:
        current = await self.sessions.get_for_job(job_id)
        if current is None:
            return None
        updated = await self.sessions.update(
            current.id,
            status=status.value,
            last_active_at=datetime.now(timezone.utc),
        )
        await self.events.append(
            f"SESSION_{status.value}",
            job_id=current.job_id,
            project_id=current.project_id,
            host_id=current.host_id,
            payload={
                "session_id": current.id,
                "external_session_id": current.external_session_id,
            },
        )
        return updated
