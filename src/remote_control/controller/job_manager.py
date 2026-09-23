from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from remote_control.controller.states import JobState, TERMINAL_STATES, validate_transition
from remote_control.hosts.registry import HostRegistry
from remote_control.hosts.router import HostRouter
from remote_control.projects.registry import ProjectRegistry
from remote_control.runners.base import AgentRunner, RunHandle
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import EventRepository, JobRepository

Notifier = Callable[[str, str], Awaitable[None]]


def _job_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"JOB-{timestamp}-{uuid4().hex[:6].upper()}"


class JobManager:
    def __init__(
        self,
        *,
        projects: ProjectRegistry,
        jobs: JobRepository,
        events: EventRepository,
        runner: AgentRunner,
        local_host_id: str,
        hosts: HostRegistry | None = None,
    ) -> None:
        self.projects = projects
        self.jobs = jobs
        self.events = events
        self.runner = runner
        self.local_host_id = local_host_id
        self.hosts = hosts
        self.host_router = HostRouter(hosts) if hosts is not None else None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._handles: dict[str, RunHandle] = {}
        self._notifier: Notifier | None = None

    def set_notifier(self, notifier: Notifier | None) -> None:
        self._notifier = notifier

    async def create(
        self,
        *,
        project_id: str,
        instruction: str,
        requested_by_channel: str,
        requested_by_user: str,
        requested_host: str = "auto",
    ) -> JobRecord:
        project = self.projects.get(project_id)
        assigned_host = await self._resolve_host(project_id, requested_host)
        job = JobRecord(
            id=_job_id(),
            project_id=project.id,
            requested_by_channel=requested_by_channel,
            requested_by_user=requested_by_user,
            requested_host=requested_host,
            assigned_host=None,
            instruction=instruction,
            state=JobState.QUEUED.value,
        )
        await self.jobs.add(job)
        await self.events.append(
            "JOB_CREATED",
            job_id=job.id,
            project_id=project.id,
            payload={"requested_host": requested_host},
        )
        await self._transition(job.id, JobState.ASSIGNED, assigned_host=assigned_host)
        task = asyncio.create_task(self._execute(job.id), name=f"job:{job.id}")
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))
        return await self.require(job.id)

    async def cancel(self, job_id: str) -> JobRecord:
        job = await self.require(job_id)
        current = JobState(job.state)
        if current in TERMINAL_STATES:
            return job

        handle = self._handles.get(job_id)
        if handle is not None:
            await handle.cancel()

        latest = await self.require(job_id)
        if JobState(latest.state) in TERMINAL_STATES:
            return latest

        await self._transition(job_id, JobState.CANCELLED)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return await self.require(job_id)

    async def require(self, job_id: str) -> JobRecord:
        job = await self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    async def list(self, limit: int = 50) -> list[JobRecord]:
        return await self.jobs.list(limit=limit)

    async def active_for_user(self, user_id: str) -> list[JobRecord]:
        return await self.jobs.list_active_for_user(user_id)

    async def _resolve_host(self, project_id: str, requested_host: str) -> str:
        project = self.projects.get(project_id)
        if self.host_router is not None:
            return await self.host_router.choose(project, requested_host)

        if requested_host == "auto":
            host = project.default_host or self.local_host_id
        else:
            host = requested_host
        if host not in project.allowed_hosts:
            raise ValueError(f"host {host!r} is not allowed for project {project_id!r}")
        if host != self.local_host_id:
            raise ValueError(
                f"R0 supports only local host {self.local_host_id!r}; "
                f"remote host {host!r} requires the R1 host registry"
            )
        project.path_for(host)
        return host

    async def _execute(self, job_id: str) -> None:
        try:
            job = await self._transition(job_id, JobState.STARTING)
            project = self.projects.get(job.project_id)
            assert job.assigned_host is not None
            working_directory = Path(project.path_for(job.assigned_host)).expanduser()

            async def on_event(event: dict) -> None:
                await self.events.append(
                    "AGENT_EVENT",
                    job_id=job_id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload=_small_event(event),
                )

            handle = await self.runner.start(
                project_id=job.project_id,
                instruction=job.instruction,
                working_directory=working_directory,
                host_id=job.assigned_host,
                on_event=on_event,
            )
            self._handles[job_id] = handle
            await self.jobs.update(job_id, pid=handle.pid, external_session_id=handle.session_id)
            await self._transition(job_id, JobState.RUNNING)

            result = await handle.wait()
            current = await self.require(job_id)
            if JobState(current.state) == JobState.CANCELLED:
                return

            await self.jobs.update(
                job_id,
                external_session_id=result.session_id,
                result=result.final_message,
            )
            if result.returncode == 0:
                await self._transition(job_id, JobState.COMPLETED)
                await self._notify(job_id, "✅ 작업이 완료되었습니다.")
            else:
                await self.jobs.update(job_id, error=result.final_message)
                await self._transition(job_id, JobState.FAILED)
                await self._notify(job_id, "❌ Agent 실행이 실패했습니다.")
        except asyncio.CancelledError:
            current = await self.require(job_id)
            if JobState(current.state) not in TERMINAL_STATES:
                await self._transition(job_id, JobState.CANCELLED)
            raise
        except Exception as exc:
            current = await self.require(job_id)
            if JobState(current.state) not in TERMINAL_STATES:
                await self.jobs.update(job_id, error=str(exc))
                await self._transition(job_id, JobState.FAILED)
                await self._notify(job_id, f"❌ 작업 실패: {exc}")
        finally:
            self._handles.pop(job_id, None)

    async def _transition(
        self,
        job_id: str,
        target: JobState,
        **changes: object,
    ) -> JobRecord:
        job = await self.require(job_id)
        current = JobState(job.state)
        validate_transition(current, target)
        updated = await self.jobs.update(job_id, state=target.value, **changes)
        await self.events.append(
            f"JOB_{target.value}",
            job_id=job_id,
            project_id=job.project_id,
            host_id=updated.assigned_host,
            payload={"from": current.value, "to": target.value},
        )
        return updated

    async def _notify(self, job_id: str, message: str) -> None:
        if self._notifier is None:
            return
        job = await self.require(job_id)
        if job.requested_by_channel == "telegram":
            await self._notifier(job.requested_by_user, f"[{job.project_id} / {job.id}]\n{message}")


def _small_event(event: dict) -> dict:
    allowed = {}
    for key in ("type", "event_type", "message", "text", "thread_id", "session_id"):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[key] = value
    return allowed
