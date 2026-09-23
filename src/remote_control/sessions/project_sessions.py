from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from remote_control.storage.models import ProjectSessionRecord
from remote_control.storage.repositories import EventRepository, ProjectSessionRepository


class ProjectSessionStatus(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


class ProjectSessionBusyError(ValueError):
    def __init__(self, session_id: str, locked_by_job_id: str) -> None:
        self.session_id = session_id
        self.locked_by_job_id = locked_by_job_id
        super().__init__(
            f"project session {session_id} is busy with {locked_by_job_id}"
        )


def _project_session_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"PSESSION-{timestamp}-{uuid4().hex[:6].upper()}"


class ProjectSessionRegistry:
    """Owns cross-Job Codex context for one project and one Remote Control user."""

    def __init__(
        self,
        *,
        sessions: ProjectSessionRepository,
        events: EventRepository,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, session_id: str) -> ProjectSessionRecord | None:
        return await self.sessions.get(session_id)

    async def active_for(
        self,
        project_id: str,
        owner_user_id: str,
    ) -> ProjectSessionRecord | None:
        return await self.sessions.active_for(project_id, owner_user_id)

    async def list_for_user(
        self,
        owner_user_id: str,
        *,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[ProjectSessionRecord]:
        return await self.sessions.list_for_user(
            owner_user_id,
            project_id=project_id,
            limit=limit,
        )

    async def acquire(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        job_id: str,
        seed_external_session_id: str | None = None,
        seed_host_id: str | None = None,
    ) -> ProjectSessionRecord:
        key = (project_id, owner_user_id)
        async with self._locks[key]:
            current = await self.sessions.active_for(project_id, owner_user_id)
            now = datetime.now(timezone.utc)
            if current is None:
                current = await self.sessions.add(
                    ProjectSessionRecord(
                        id=_project_session_id(),
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        external_session_id=seed_external_session_id,
                        host_id=seed_host_id,
                        status=ProjectSessionStatus.IDLE.value,
                        last_job_id=None,
                        locked_by_job_id=None,
                        last_active_at=now,
                    )
                )
                await self.events.append(
                    "PROJECT_SESSION_CREATED",
                    project_id=project_id,
                    payload={
                        "project_session_id": current.id,
                        "owner_user_id": owner_user_id,
                        "seeded_external_session_id": seed_external_session_id,
                        "seed_host_id": seed_host_id,
                    },
                )

            if current.locked_by_job_id and current.locked_by_job_id != job_id:
                raise ProjectSessionBusyError(current.id, current.locked_by_job_id)

            updated = await self.sessions.update(
                current.id,
                status=ProjectSessionStatus.ACTIVE.value,
                last_job_id=job_id,
                locked_by_job_id=job_id,
                last_active_at=now,
            )
            await self.events.append(
                "PROJECT_SESSION_ACQUIRED",
                job_id=job_id,
                project_id=project_id,
                host_id=updated.host_id,
                payload={
                    "project_session_id": updated.id,
                    "external_session_id": updated.external_session_id,
                    "owner_user_id": owner_user_id,
                },
            )
            return updated

    async def bind_for_job(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        job_id: str,
        external_session_id: str,
        host_id: str,
    ) -> ProjectSessionRecord:
        key = (project_id, owner_user_id)
        async with self._locks[key]:
            current = await self.sessions.active_for(project_id, owner_user_id)
            if current is None:
                now = datetime.now(timezone.utc)
                current = await self.sessions.add(
                    ProjectSessionRecord(
                        id=_project_session_id(),
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        external_session_id=external_session_id,
                        host_id=host_id,
                        status=ProjectSessionStatus.ACTIVE.value,
                        last_job_id=job_id,
                        locked_by_job_id=job_id,
                        last_active_at=now,
                    )
                )
                await self.events.append(
                    "PROJECT_SESSION_MIGRATED",
                    job_id=job_id,
                    project_id=project_id,
                    host_id=host_id,
                    payload={
                        "project_session_id": current.id,
                        "external_session_id": external_session_id,
                        "owner_user_id": owner_user_id,
                    },
                )
            if current.locked_by_job_id not in {None, job_id}:
                raise ProjectSessionBusyError(
                    current.id,
                    current.locked_by_job_id,
                )
            previous = current.external_session_id
            updated = await self.sessions.update(
                current.id,
                external_session_id=external_session_id,
                host_id=host_id,
                status=ProjectSessionStatus.ACTIVE.value,
                last_job_id=job_id,
                locked_by_job_id=job_id,
                last_active_at=datetime.now(timezone.utc),
            )
            if previous != external_session_id:
                await self.events.append(
                    "PROJECT_SESSION_BOUND",
                    job_id=job_id,
                    project_id=project_id,
                    host_id=host_id,
                    payload={
                        "project_session_id": updated.id,
                        "previous_external_session_id": previous,
                        "external_session_id": external_session_id,
                        "owner_user_id": owner_user_id,
                    },
                )
            return updated

    async def release_for_job(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        job_id: str,
    ) -> ProjectSessionRecord | None:
        key = (project_id, owner_user_id)
        async with self._locks[key]:
            current = await self.sessions.active_for(project_id, owner_user_id)
            if current is None:
                return None
            if current.locked_by_job_id not in {None, job_id}:
                return current
            updated = await self.sessions.update(
                current.id,
                status=ProjectSessionStatus.IDLE.value,
                last_job_id=job_id,
                locked_by_job_id=None,
                last_active_at=datetime.now(timezone.utc),
            )
            await self.events.append(
                "PROJECT_SESSION_RELEASED",
                job_id=job_id,
                project_id=project_id,
                host_id=updated.host_id,
                payload={
                    "project_session_id": updated.id,
                    "external_session_id": updated.external_session_id,
                    "owner_user_id": owner_user_id,
                },
            )
            return updated

    async def new_session(
        self,
        *,
        project_id: str,
        owner_user_id: str,
    ) -> ProjectSessionRecord:
        key = (project_id, owner_user_id)
        async with self._locks[key]:
            current = await self.sessions.active_for(project_id, owner_user_id)
            now = datetime.now(timezone.utc)
            if current is not None:
                if current.locked_by_job_id:
                    raise ProjectSessionBusyError(
                        current.id,
                        current.locked_by_job_id,
                    )
                await self.sessions.update(
                    current.id,
                    status=ProjectSessionStatus.CLOSED.value,
                    closed_at=now,
                    last_active_at=now,
                )
                await self.events.append(
                    "PROJECT_SESSION_CLOSED",
                    project_id=project_id,
                    host_id=current.host_id,
                    payload={
                        "project_session_id": current.id,
                        "external_session_id": current.external_session_id,
                        "owner_user_id": owner_user_id,
                    },
                )

            created = await self.sessions.add(
                ProjectSessionRecord(
                    id=_project_session_id(),
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    external_session_id=None,
                    host_id=None,
                    status=ProjectSessionStatus.IDLE.value,
                    last_job_id=None,
                    locked_by_job_id=None,
                    last_active_at=now,
                )
            )
            await self.events.append(
                "PROJECT_SESSION_CREATED",
                project_id=project_id,
                payload={
                    "project_session_id": created.id,
                    "owner_user_id": owner_user_id,
                    "rollover": current is not None,
                },
            )
            return created
