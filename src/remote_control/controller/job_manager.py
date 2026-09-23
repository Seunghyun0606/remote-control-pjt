from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from remote_control.controller.states import JobState, TERMINAL_STATES, validate_transition
from remote_control.feedback import FeedbackPolicy, FeedbackThrottler
from remote_control.hosts.registry import HostRegistry
from remote_control.hosts.router import HostRouter
from remote_control.projects.registry import ProjectRegistry
from remote_control.runners.base import AgentRunResult, AgentRunner, RunHandle
from remote_control.runners.codex import extract_session_id
from remote_control.sessions.registry import SessionRegistry, SessionStatus
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import EventRepository, JobRepository

Notifier = Callable[[str, str], Awaitable[None]]

RESUME_INSTRUCTION = (
    "Resume this job from the existing repository and Codex session state. "
    "Inspect current changes before continuing, then finish the next appropriate step."
)


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
        sessions: SessionRegistry | None = None,
        progress_interval_seconds: int = 300,
    ) -> None:
        self.projects = projects
        self.jobs = jobs
        self.events = events
        self.runner = runner
        self.local_host_id = local_host_id
        self.hosts = hosts
        self.sessions = sessions
        self.host_router = HostRouter(hosts) if hosts is not None else None
        self.feedback_policy = FeedbackPolicy()
        self.feedback_throttler = FeedbackThrottler(progress_interval_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._handles: dict[str, RunHandle] = {}
        self._steering: dict[str, list[str]] = defaultdict(list)
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
        self._start_task(job.id, self._execute_new(job.id))
        return await self.require(job.id)

    async def pause(self, job_id: str) -> JobRecord:
        job = await self.require(job_id)
        if JobState(job.state) != JobState.RUNNING:
            raise ValueError(f"job {job_id} is not RUNNING")
        await self._transition(job_id, JobState.PAUSED)
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.PAUSED)
        handle = self._handles.get(job_id)
        if handle is not None:
            await handle.cancel()
        return await self.require(job_id)

    async def resume(
        self,
        job_id: str,
        *,
        instruction: str = RESUME_INSTRUCTION,
    ) -> JobRecord:
        job = await self.require(job_id)
        if JobState(job.state) != JobState.PAUSED:
            raise ValueError(f"job {job_id} is not PAUSED")
        if job.assigned_host and self.hosts is not None:
            if not await self.hosts.is_online(job.assigned_host):
                raise ValueError(
                    f"host {job.assigned_host!r} is offline; WAITING_HOST recovery is Phase R4"
                )
        previous_task = self._tasks.get(job_id)
        if previous_task is not None and not previous_task.done():
            await asyncio.shield(previous_task)

        await self._transition(job_id, JobState.RUNNING)
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.ACTIVE)
        self._start_task(job.id, self._execute_resume(job.id, instruction))
        return await self.require(job.id)

    async def steer(self, job_id: str, instruction: str) -> JobRecord:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("steering instruction must not be empty")
        job = await self.require(job_id)
        if JobState(job.state) != JobState.RUNNING:
            raise ValueError(f"job {job_id} is not RUNNING")
        self._steering[job_id].append(instruction)
        await self.events.append(
            "STEERING_QUEUED",
            job_id=job.id,
            project_id=job.project_id,
            host_id=job.assigned_host,
            payload={"instruction": instruction},
        )
        return job

    async def cancel(self, job_id: str) -> JobRecord:
        job = await self.require(job_id)
        current = JobState(job.state)
        if current in TERMINAL_STATES:
            return job

        await self._transition(job_id, JobState.CANCELLED)
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.CANCELLED)

        handle = self._handles.get(job_id)
        if handle is not None:
            await handle.cancel()
        else:
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

    async def select_for_user(
        self,
        user_id: str,
        *,
        job_id: str | None = None,
        states: set[JobState] | None = None,
    ) -> JobRecord:
        if job_id is not None:
            job = await self.require(job_id)
            if job.requested_by_user != user_id:
                raise ValueError("job belongs to another user")
            if states is not None and JobState(job.state) not in states:
                allowed = ", ".join(sorted(state.value for state in states))
                raise ValueError(f"job {job_id} must be in one of: {allowed}")
            return job

        candidates = await self.active_for_user(user_id)
        if states is not None:
            candidates = [job for job in candidates if JobState(job.state) in states]
        if not candidates:
            raise ValueError("matching active job not found")
        if len(candidates) > 1:
            raise ValueError("multiple jobs match; specify a job id")
        return candidates[0]

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

    def _start_task(self, job_id: str, coroutine) -> None:
        task = asyncio.create_task(coroutine, name=f"job:{job_id}")
        self._tasks[job_id] = task

        def cleanup(done_task: asyncio.Task[None]) -> None:
            if self._tasks.get(job_id) is done_task:
                self._tasks.pop(job_id, None)

        task.add_done_callback(cleanup)

    async def _execute_new(self, job_id: str) -> None:
        try:
            job = await self._transition(job_id, JobState.STARTING)
            handle = await self._start_new_turn(job, job.instruction)
            await self._set_handle(job_id, handle)
            await self._transition(job_id, JobState.RUNNING)
            result = await self._await_handle(job_id, handle)
            if result is None:
                return
            await self._finish_or_continue(job_id, result)
        except asyncio.CancelledError:
            await self._handle_cancelled_task(job_id)
        except Exception as exc:
            await self._fail(job_id, exc)
        finally:
            self._handles.pop(job_id, None)

    async def _execute_resume(self, job_id: str, instruction: str) -> None:
        try:
            result = await self._resume_or_fallback(job_id, instruction)
            if result is None:
                return
            await self._finish_or_continue(job_id, result)
        except asyncio.CancelledError:
            await self._handle_cancelled_task(job_id)
        except Exception as exc:
            await self._fail(job_id, exc)
        finally:
            self._handles.pop(job_id, None)

    async def _finish_or_continue(
        self,
        job_id: str,
        result: AgentRunResult,
    ) -> None:
        current_result = result
        while True:
            if current_result.returncode != 0:
                await self.jobs.update(job_id, error=current_result.final_message)
                if self.sessions is not None:
                    await self.sessions.mark(job_id, SessionStatus.FAILED)
                await self._transition(job_id, JobState.FAILED)
                await self._notify(
                    job_id,
                    "❌ Agent 실행이 실패했습니다."
                    + _optional_detail(current_result.final_message),
                )
                return

            steering = self._drain_steering(job_id)
            if steering is None:
                await self.jobs.update(
                    job_id,
                    result=current_result.final_message,
                    error=None,
                )
                if self.sessions is not None:
                    await self.sessions.mark(job_id, SessionStatus.IDLE)
                await self._transition(job_id, JobState.COMPLETED)
                await self._notify(
                    job_id,
                    "✅ 작업이 완료되었습니다."
                    + _optional_detail(current_result.final_message),
                )
                return

            job = await self.require(job_id)
            await self.events.append(
                "STEERING_APPLIED",
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={"instruction": steering},
            )
            await self._notify(job_id, "↪ 추가 지시를 기존 Codex session에 전달합니다.")
            next_result = await self._resume_or_fallback(
                job_id,
                steering,
                steering=True,
            )
            if next_result is None:
                return
            current_result = next_result

    async def _resume_or_fallback(
        self,
        job_id: str,
        instruction: str,
        *,
        steering: bool = False,
    ) -> AgentRunResult | None:
        job = await self.require(job_id)
        if JobState(job.state) != JobState.RUNNING:
            return None
        project = self.projects.get(job.project_id)
        assert job.assigned_host is not None
        working_directory = Path(project.path_for(job.assigned_host)).expanduser()
        session = await self.sessions.get_for_job(job_id) if self.sessions is not None else None
        requested_session_id = (
            session.external_session_id
            if session is not None
            else job.external_session_id
        )

        if requested_session_id:
            await self.events.append(
                "SESSION_STEER_REQUESTED" if steering else "SESSION_RESUME_REQUESTED",
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={"external_session_id": requested_session_id},
            )
            try:
                operation = self.runner.steer if steering else self.runner.resume
                handle = await operation(
                    session_id=requested_session_id,
                    instruction=instruction,
                    working_directory=working_directory,
                    host_id=job.assigned_host,
                    on_event=self._event_callback(job_id),
                )
                await self._set_handle(job_id, handle)
                result = await self._await_handle(job_id, handle)
                if result is None:
                    return None
                if result.returncode == 0:
                    returned_session_id = result.session_id or (
                        await self.require(job_id)
                    ).external_session_id
                    if (
                        returned_session_id
                        and returned_session_id != requested_session_id
                    ):
                        await self.events.append(
                            "SESSION_RESUME_REBOUND",
                            job_id=job_id,
                            project_id=job.project_id,
                            host_id=job.assigned_host,
                            payload={
                                "requested_external_session_id": requested_session_id,
                                "returned_external_session_id": returned_session_id,
                            },
                        )
                    return result

                await self.events.append(
                    "SESSION_RESUME_FAILED",
                    job_id=job_id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "external_session_id": requested_session_id,
                        "returncode": result.returncode,
                        "error": result.final_message,
                    },
                )
            except Exception as exc:
                await self.events.append(
                    "SESSION_RESUME_FAILED",
                    job_id=job_id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "external_session_id": requested_session_id,
                        "error": str(exc),
                    },
                )

        current = await self.require(job_id)
        if JobState(current.state) != JobState.RUNNING:
            return None

        fallback_instruction = (
            "The previous Codex session is unavailable. Reload repository state, "
            "inspect current changes and continue safely from the filesystem state.\n\n"
            f"User instruction:\n{instruction}"
        )
        await self.events.append(
            "SESSION_FALLBACK_NEW",
            job_id=job_id,
            project_id=job.project_id,
            host_id=job.assigned_host,
            payload={"had_session": bool(requested_session_id)},
        )
        handle = await self._start_new_turn(job, fallback_instruction)
        await self._set_handle(job_id, handle)
        return await self._await_handle(job_id, handle)

    async def _start_new_turn(self, job: JobRecord, instruction: str) -> RunHandle:
        project = self.projects.get(job.project_id)
        assert job.assigned_host is not None
        working_directory = Path(project.path_for(job.assigned_host)).expanduser()
        return await self.runner.start(
            project_id=job.project_id,
            instruction=instruction,
            working_directory=working_directory,
            host_id=job.assigned_host,
            on_event=self._event_callback(job.id),
        )

    async def _set_handle(self, job_id: str, handle: RunHandle) -> None:
        self._handles[job_id] = handle
        changes: dict[str, object] = {"pid": handle.pid}
        if handle.session_id:
            changes["external_session_id"] = handle.session_id
        await self.jobs.update(job_id, **changes)

    async def _await_handle(
        self,
        job_id: str,
        handle: RunHandle,
    ) -> AgentRunResult | None:
        try:
            result = await handle.wait()
        except asyncio.CancelledError:
            current = await self.require(job_id)
            if JobState(current.state) in {JobState.PAUSED, JobState.CANCELLED}:
                return None
            raise

        current = await self.require(job_id)
        if JobState(current.state) in {JobState.PAUSED, JobState.CANCELLED}:
            return None

        if result.session_id:
            await self._record_session(job_id, result.session_id)
        await self.jobs.update(
            job_id,
            external_session_id=result.session_id or current.external_session_id,
            result=result.final_message,
        )
        return result

    def _event_callback(self, job_id: str):
        async def on_event(event: dict) -> None:
            job = await self.require(job_id)
            await self.events.append(
                "AGENT_EVENT",
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload=_small_event(event),
            )

            session_id = extract_session_id(event)
            if session_id:
                await self._record_session(job_id, session_id)

            feedback = self.feedback_policy.classify(event)
            if (
                feedback is not None
                and self.feedback_throttler.allow(job_id, feedback)
            ):
                await self.events.append(
                    "FEEDBACK_SENT",
                    job_id=job_id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "level": feedback.level.value,
                        "text": feedback.text,
                    },
                )
                await self._notify(job_id, f"⏳ 진행\n\n{feedback.text}")

        return on_event

    async def _record_session(self, job_id: str, external_session_id: str) -> None:
        job = await self.require(job_id)
        if job.assigned_host is None:
            return
        if job.external_session_id != external_session_id:
            await self.jobs.update(job_id, external_session_id=external_session_id)
        if self.sessions is not None:
            await self.sessions.record(
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                external_session_id=external_session_id,
                status=SessionStatus.ACTIVE,
            )

    def _drain_steering(self, job_id: str) -> str | None:
        messages = self._steering.pop(job_id, [])
        if not messages:
            return None
        if len(messages) == 1:
            return messages[0]
        joined = "\n".join(
            f"{index}. {message}" for index, message in enumerate(messages, start=1)
        )
        return f"Apply these user steering instructions together:\n{joined}"

    async def _handle_cancelled_task(self, job_id: str) -> None:
        current = await self.require(job_id)
        state = JobState(current.state)
        if state in {JobState.PAUSED, JobState.CANCELLED}:
            return
        if state not in TERMINAL_STATES:
            await self._transition(job_id, JobState.CANCELLED)

    async def _fail(self, job_id: str, exc: Exception) -> None:
        current = await self.require(job_id)
        state = JobState(current.state)
        if state in TERMINAL_STATES or state == JobState.PAUSED:
            return
        await self.jobs.update(job_id, error=str(exc))
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.FAILED)
        await self._transition(job_id, JobState.FAILED)
        await self._notify(job_id, f"❌ 작업 실패: {exc}")

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
            await self._notifier(
                job.requested_by_user,
                f"[{job.project_id} / {job.id}]\n{message}",
            )


def _small_event(event: dict) -> dict:
    allowed: dict = {}
    for key in (
        "type",
        "event_type",
        "message",
        "text",
        "error",
        "thread_id",
        "session_id",
    ):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[key] = value
    item = event.get("item")
    if isinstance(item, dict):
        clean_item = {}
        for key in ("type", "text", "content", "command", "status", "exit_code"):
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean_item[key] = value
        allowed["item"] = clean_item
    return allowed


def _optional_detail(message: str | None) -> str:
    if not message:
        return ""
    text = message.strip()
    if not text:
        return ""
    if len(text) > 1500:
        text = text[:1499] + "…"
    return f"\n\n{text}"
