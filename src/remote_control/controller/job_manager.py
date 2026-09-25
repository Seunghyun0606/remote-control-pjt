from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from remote_control.approvals.registry import ApprovalPrompt, ApprovalRegistry
from remote_control.controller.states import JobState, TERMINAL_STATES, validate_transition
from remote_control.feedback import FeedbackPolicy, FeedbackThrottler
from remote_control.hosts.registry import HostRegistry
from remote_control.hosts.router import HostRouter, HostUnavailable
from remote_control.human_gate import (
    HumanGateRequest,
    extract_human_gate,
    extract_human_gate_from_text,
    human_gate_protocol_instruction,
)
from remote_control.projects.adapters import NoProjectWork, ProjectAdapterRegistry
from remote_control.projects.registry import ProjectRegistry
from remote_control.recovery.models import RecoveryKind, RecoveryMode
from remote_control.recovery.quota import (
    QuotaSignal,
    detect_quota_event,
    detect_quota_text,
    retry_at,
)
from remote_control.runners.base import AgentRunResult, AgentRunner, RunHandle
from remote_control.runners.codex import extract_session_id
from remote_control.sessions.project_sessions import ProjectSessionRegistry
from remote_control.sessions.registry import SessionRegistry, SessionStatus
from remote_control.storage.models import ApprovalRecord, JobRecord, ProjectWorkRecord, RecoveryRecord
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    ProjectWorkRepository,
    RecoveryRepository,
)
from remote_control.transport.runner_ws import RunnerGateway

Notifier = Callable[[str, str], Awaitable[None]]
ApprovalNotifier = Callable[[str, ApprovalPrompt], Awaitable[None]]

logger = logging.getLogger(__name__)

_NOTIFICATION_RETRY_DELAYS = (0.5, 1.0)

RESUME_INSTRUCTION = (
    "Resume this job from the existing repository and Codex session state. "
    "Inspect current changes before continuing, then finish the next appropriate step."
)
QUOTA_RESUME_INSTRUCTION = (
    "The previous turn stopped because the Codex usage quota was unavailable. "
    "Re-inspect the repository state, resume the unfinished work safely, and do not "
    "repeat changes that are already present."
)
RESTART_RESUME_INSTRUCTION = (
    "The Controller restarted while this job was active. Re-inspect the repository "
    "and session state, then continue the unfinished work safely without duplicating "
    "already-completed changes."
)
RETRY_RESUME_INSTRUCTION = (
    "Retry the failed Remote Control Job using this existing Codex session. "
    "Re-inspect the repository state before making changes, do not duplicate completed work, "
    "and continue the original instruction from the current filesystem state."
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
        project_sessions: ProjectSessionRegistry | None = None,
        approvals: ApprovalRegistry | None = None,
        recovery: RecoveryRepository | None = None,
        project_adapters: ProjectAdapterRegistry | None = None,
        project_work: ProjectWorkRepository | None = None,
        progress_interval_seconds: int = 300,
        quota_retry_initial_seconds: int = 1800,
        quota_retry_max_seconds: int = 7200,
        quota_reset_grace_seconds: int = 600,
        restart_grace_seconds: int = 10,
    ) -> None:
        self.projects = projects
        self.jobs = jobs
        self.events = events
        self.runner = runner
        self.local_host_id = local_host_id
        self.hosts = hosts
        self.sessions = sessions
        self.project_sessions = project_sessions
        self.approvals = approvals
        self.recovery = recovery
        self.project_adapters = project_adapters
        self.project_work = project_work
        self.host_router = HostRouter(hosts) if hosts is not None else None
        self.feedback_policy = FeedbackPolicy()
        self.feedback_throttler = FeedbackThrottler(progress_interval_seconds)
        self.quota_retry_initial_seconds = max(quota_retry_initial_seconds, 1)
        self.quota_retry_max_seconds = max(
            quota_retry_max_seconds,
            self.quota_retry_initial_seconds,
        )
        self.quota_reset_grace_seconds = max(quota_reset_grace_seconds, 0)
        self.restart_grace_seconds = max(restart_grace_seconds, 0)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._handles: dict[str, RunHandle] = {}
        self._steering: dict[str, list[str]] = defaultdict(list)
        self._job_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._notifiers: dict[str, Notifier] = {}
        self._approval_notifiers: dict[str, ApprovalNotifier] = {}

    def set_notifier(
        self,
        notifier: Notifier | None,
        *,
        channel: str = "telegram",
    ) -> None:
        if notifier is None:
            self._notifiers.pop(channel, None)
            return
        self._notifiers[channel] = notifier

    def set_approval_notifier(
        self,
        notifier: ApprovalNotifier | None,
        *,
        channel: str = "telegram",
    ) -> None:
        if notifier is None:
            self._approval_notifiers.pop(channel, None)
            return
        self._approval_notifiers[channel] = notifier

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
        self._validate_requested_host(project_id, requested_host)
        job_id = _job_id()
        project_session = None
        external_session_id = None
        if self.project_sessions is not None:
            legacy = await self.jobs.latest_for_user_project(
                requested_by_user,
                project.id,
            )
            project_session = await self.project_sessions.acquire(
                project_id=project.id,
                owner_user_id=requested_by_user,
                job_id=job_id,
                seed_external_session_id=(
                    legacy.external_session_id if legacy is not None else None
                ),
                seed_host_id=legacy.assigned_host if legacy is not None else None,
            )
            external_session_id = project_session.external_session_id

        job = JobRecord(
            id=job_id,
            project_id=project.id,
            requested_by_channel=requested_by_channel,
            requested_by_user=requested_by_user,
            requested_host=requested_host,
            assigned_host=None,
            instruction=instruction,
            state=JobState.QUEUED.value,
            external_session_id=external_session_id,
        )
        try:
            await self.jobs.add(job)
        except Exception:
            if self.project_sessions is not None and project_session is not None:
                await self.project_sessions.release_for_job(
                    project_id=project.id,
                    owner_user_id=requested_by_user,
                    job_id=job_id,
                )
            raise
        await self.events.append(
            "JOB_CREATED",
            job_id=job.id,
            project_id=project.id,
            payload={
                "requested_host": requested_host,
                "project_session_id": project_session.id if project_session else None,
                "external_session_id": external_session_id,
            },
        )

        effective_host = requested_host
        if (
            requested_host == "auto"
            and project_session is not None
            and project_session.external_session_id
            and project_session.host_id
        ):
            effective_host = project_session.host_id

        try:
            assigned_host = await self._resolve_host(project_id, effective_host)
        except HostUnavailable as exc:
            await self._enter_host_wait(
                job.id,
                mode=RecoveryMode.START,
                error=str(exc),
                assigned_host=effective_host if effective_host != "auto" else None,
            )
            return await self.require(job.id)

        await self._transition(job.id, JobState.ASSIGNED, assigned_host=assigned_host)
        self._start_task(job.id, self._execute_new(job.id))
        return await self.require(job.id)

    async def retry_failed(self, job_id: str) -> JobRecord:
        original = await self.require(job_id)
        if JobState(original.state) != JobState.FAILED:
            raise ValueError(f"job {job_id} is not FAILED")

        project = self.projects.get(original.project_id)
        if project.adapter == "project_os":
            raise ValueError(
                "Project OS Job은 /retry로 복제하지 않습니다. "
                "/run으로 현재 canonical task state를 다시 평가하세요."
            )

        retry_id = _job_id()
        project_session = None
        external_session_id = original.external_session_id
        if self.project_sessions is not None:
            project_session = await self.project_sessions.acquire(
                project_id=original.project_id,
                owner_user_id=original.requested_by_user,
                job_id=retry_id,
                seed_external_session_id=original.external_session_id,
                seed_host_id=original.assigned_host,
            )
            external_session_id = (
                project_session.external_session_id or original.external_session_id
            )

        retry = JobRecord(
            id=retry_id,
            project_id=original.project_id,
            requested_by_channel=original.requested_by_channel,
            requested_by_user=original.requested_by_user,
            requested_host=original.requested_host,
            assigned_host=None,
            instruction=original.instruction,
            state=JobState.QUEUED.value,
            external_session_id=external_session_id,
        )
        try:
            await self.jobs.add(retry)
        except Exception:
            if self.project_sessions is not None and project_session is not None:
                await self.project_sessions.release_for_job(
                    project_id=original.project_id,
                    owner_user_id=original.requested_by_user,
                    job_id=retry_id,
                )
            raise
        await self.events.append(
            "JOB_RETRY_CREATED",
            job_id=retry.id,
            project_id=retry.project_id,
            payload={
                "retry_of": original.id,
                "external_session_id": original.external_session_id,
            },
        )

        mode = (
            RecoveryMode.RESUME
            if original.external_session_id
            else RecoveryMode.START
        )
        try:
            assigned_host = await self._resolve_host(
                retry.project_id,
                retry.requested_host,
            )
        except HostUnavailable as exc:
            await self._enter_host_wait(
                retry.id,
                mode=mode,
                error=str(exc),
                assigned_host=(
                    retry.requested_host
                    if retry.requested_host != "auto"
                    else None
                ),
                resume_instruction=(
                    RETRY_RESUME_INSTRUCTION
                    if mode == RecoveryMode.RESUME
                    else None
                ),
            )
            return await self.require(retry.id)

        await self._transition(
            retry.id,
            JobState.ASSIGNED,
            assigned_host=assigned_host,
        )
        if original.external_session_id:
            self._start_task(retry.id, self._execute_retry(retry.id))
        else:
            self._start_task(retry.id, self._execute_new(retry.id))
        return await self.require(retry.id)

    async def retry_waiting(self, job_id: str) -> JobRecord:
        job = await self.require(job_id)
        state = JobState(job.state)
        if state not in {JobState.WAITING_HOST, JobState.WAITING_QUOTA}:
            raise ValueError(
                f"job {job_id} must be WAITING_HOST or WAITING_QUOTA"
            )
        if self.recovery is None:
            raise ValueError("Recovery Registry가 활성화되지 않았습니다.")

        async with self._job_locks[f"manual-retry:{job_id}"]:
            job = await self.require(job_id)
            state = JobState(job.state)
            if state not in {JobState.WAITING_HOST, JobState.WAITING_QUOTA}:
                return job

            record = await self.recovery.get(job.id)
            if record is None:
                mode = (
                    RecoveryMode.RESUME
                    if job.external_session_id
                    else RecoveryMode.START
                )
                record = await self.recovery.upsert(
                    job.id,
                    kind=(
                        RecoveryKind.QUOTA.value
                        if state == JobState.WAITING_QUOTA
                        else RecoveryKind.HOST.value
                    ),
                    mode=mode.value,
                    attempt_count=0,
                    next_retry_at=datetime.now(timezone.utc),
                    execution_id=None,
                    resume_instruction=(
                        QUOTA_RESUME_INSTRUCTION
                        if state == JobState.WAITING_QUOTA
                        else (
                            RESTART_RESUME_INSTRUCTION
                            if mode == RecoveryMode.RESUME
                            else None
                        )
                    ),
                    last_error="manual retry reconstructed missing recovery metadata",
                )

            await self.events.append(
                "WAITING_JOB_RETRY_REQUESTED",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={
                    "state": state.value,
                    "kind": record.kind,
                    "mode": record.mode,
                    "forced": True,
                },
            )

            if state == JobState.WAITING_QUOTA:
                resumed = await self._retry_quota(job, record)
            else:
                resumed = await self._retry_host(job, record)

            if not resumed:
                await self.events.append(
                    "WAITING_JOB_RETRY_DEFERRED",
                    job_id=job.id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "state": state.value,
                        "reason": "required host is still unavailable",
                    },
                )
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

        previous_task = self._tasks.get(job_id)
        if previous_task is not None and not previous_task.done():
            await asyncio.shield(previous_task)

        if job.assigned_host and self.hosts is not None:
            if not await self.hosts.is_online(job.assigned_host):
                await self._enter_host_wait(
                    job_id,
                    mode=RecoveryMode.RESUME,
                    error=f"host {job.assigned_host!r} is offline",
                    assigned_host=job.assigned_host,
                    resume_instruction=instruction,
                )
                return await self.require(job_id)

        await self._transition(job_id, JobState.RUNNING)
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.ACTIVE)
        self._start_task(job.id, self._execute_resume(job.id, instruction))
        return await self.require(job.id)

    async def steer(self, job_id: str, instruction: str) -> JobRecord:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("steering instruction must not be empty")
        async with self._job_locks[job_id]:
            job = await self.require(job_id)
            state = JobState(job.state)
            steerable = {JobState.ASSIGNED, JobState.STARTING, JobState.RUNNING}
            if state not in steerable:
                raise ValueError(
                    f"job {job_id} cannot accept steering in state {state.value}"
                )
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
        async with self._job_locks[f"cancel-request:{job_id}"]:
            job = await self.require(job_id)
            state = JobState(job.state)
            if state in TERMINAL_STATES:
                return job

            if state != JobState.CANCELLING:
                await self._transition(job_id, JobState.CANCELLING)
                if self.sessions is not None:
                    await self.sessions.mark(job_id, SessionStatus.CANCELLING)
                if self.approvals is not None:
                    await self.approvals.cancel_for_job(job_id)

            handle = self._handles.get(job_id)
            if handle is not None:
                await self._persist_cancel_intent(job_id, handle)
                try:
                    await handle.cancel()
                except (ConnectionError, TimeoutError) as exc:
                    await self._record_cancel_pending(job_id, handle, exc)
                    return await self.require(job_id)
                await self._finalize_cancellation(job_id)
                return await self.require(job_id)

            task = self._tasks.get(job_id)
            if task is not None and not task.done() and task is not asyncio.current_task():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass

            current = await self.require(job_id)
            if JobState(current.state) == JobState.CANCELLING:
                recovery = await self.recovery.get(job_id) if self.recovery is not None else None
                if (
                    current.assigned_host
                    and current.assigned_host != self.local_host_id
                    and recovery is not None
                    and recovery.execution_id
                ):
                    await self.recovery.upsert(
                        job_id,
                        kind=recovery.kind,
                        mode=RecoveryMode.CANCEL.value,
                        attempt_count=recovery.attempt_count,
                        next_retry_at=None,
                        execution_id=recovery.execution_id,
                        resume_instruction=None,
                        last_error="waiting for runner reconnect to confirm cancellation",
                    )
                    await self.events.append(
                        "CANCEL_PENDING",
                        job_id=job_id,
                        project_id=current.project_id,
                        host_id=current.assigned_host,
                        payload={
                            "execution_id": recovery.execution_id,
                            "error": "waiting for runner reconnect to confirm cancellation",
                        },
                    )
                    return await self.require(job_id)
                await self._finalize_cancellation(job_id)
            return await self.require(job_id)

    async def require(self, job_id: str) -> JobRecord:
        job = await self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    async def list(self, limit: int = 50) -> list[JobRecord]:
        return await self.jobs.list(limit=limit)

    async def wait_until_idle(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is None or task.done() or task is asyncio.current_task():
            return
        await asyncio.shield(task)

    async def active_for_user(
        self,
        user_id: str,
        *,
        project_id: str | None = None,
    ) -> list[JobRecord]:
        return await self.jobs.list_active_for_user(user_id, project_id=project_id)

    async def project_work_for(self, job_id: str) -> ProjectWorkRecord | None:
        if self.project_work is None:
            return None
        return await self.project_work.get(job_id)

    async def recovery_for(self, job_id: str) -> RecoveryRecord | None:
        if self.recovery is None:
            return None
        return await self.recovery.get(job_id)

    async def pending_approvals_for_user(
        self,
        user_id: str,
        *,
        project_id: str | None = None,
    ) -> list[ApprovalRecord]:
        if self.approvals is None:
            return []
        pending = await self.approvals.pending_for_user(user_id)
        if project_id is None:
            return pending
        scoped: list[ApprovalRecord] = []
        for record in pending:
            job = await self.jobs.get(record.job_id)
            if job is not None and job.project_id == project_id:
                scoped.append(record)
        return scoped

    async def approval_details(self, approval_id: str, *, user_id: str) -> ApprovalPrompt:
        if self.approvals is None:
            raise ValueError("approval registry is not enabled")
        record = await self.approvals.get(approval_id)
        if record.requested_by_user != user_id:
            raise ValueError("approval belongs to another user")
        return self.approvals.prompt(record)

    async def match_pending_approval(
        self,
        user_id: str,
        text: str,
        *,
        project_id: str | None = None,
    ) -> ApprovalRecord | None:
        if self.approvals is None:
            return None
        pending = await self.pending_approvals_for_user(
            user_id,
            project_id=project_id,
        )
        if len(pending) != 1:
            return None
        record = pending[0]
        choice = text.strip()
        if not choice:
            return None
        keys = {option.key.casefold() for option in self.approvals.options(record)}
        if choice.casefold() not in keys:
            return None
        return await self.respond_approval(
            record.id,
            user_id=user_id,
            option_key=choice,
        )

    async def respond_approval(
        self,
        approval_id: str,
        *,
        user_id: str,
        option_key: str | None = None,
        rejected: bool = False,
        response_text: str | None = None,
    ) -> ApprovalRecord:
        if self.approvals is None:
            raise ValueError("approval registry is not enabled")

        async with self._job_locks[f"approval:{approval_id}"]:
            record = await self.approvals.get(approval_id)
            if record.requested_by_user != user_id:
                raise ValueError("approval belongs to another user")
            job = await self.require(record.job_id)
            if JobState(job.state) != JobState.WAITING_HUMAN:
                raise ValueError(f"job {job.id} is not WAITING_HUMAN")
            prompt = self.approvals.prompt(record)
            resolved = await self.approvals.resolve(
                approval_id,
                option_key=option_key,
                rejected=rejected,
                response_text=response_text,
            )
            await self.events.append(
                "HUMAN_GATE_RESOLVED",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={
                    "approval_id": approval_id,
                    "status": resolved.status,
                    "selected_option": resolved.selected_option,
                },
            )

        previous_task = self._tasks.get(job.id)
        if previous_task is not None and not previous_task.done():
            await asyncio.shield(previous_task)

        instruction = _approval_instruction(
            prompt,
            option_key=resolved.selected_option,
            rejected=rejected,
            response_text=response_text,
        )

        if job.assigned_host and self.hosts is not None:
            if not await self.hosts.is_online(job.assigned_host):
                await self._enter_host_wait(
                    job.id,
                    mode=RecoveryMode.RESUME,
                    error=f"host {job.assigned_host!r} is offline",
                    assigned_host=job.assigned_host,
                    resume_instruction=instruction,
                )
                return resolved

        await self._transition(job.id, JobState.RUNNING)
        if self.sessions is not None:
            await self.sessions.mark(job.id, SessionStatus.ACTIVE)
        self._start_task(job.id, self._execute_resume(job.id, instruction))
        return resolved

    async def select_for_user(
        self,
        user_id: str,
        *,
        job_id: str | None = None,
        states: set[JobState] | None = None,
        project_id: str | None = None,
    ) -> JobRecord:
        if job_id is not None:
            job = await self.require(job_id)
            if job.requested_by_user != user_id:
                raise ValueError("job belongs to another user")
            if project_id is not None and job.project_id != project_id:
                raise ValueError("job belongs to another project topic")
            if states is not None and JobState(job.state) not in states:
                allowed = ", ".join(sorted(state.value for state in states))
                raise ValueError(f"job {job_id} must be in one of: {allowed}")
            return job

        candidates = await self.active_for_user(user_id, project_id=project_id)
        if states is not None:
            candidates = [job for job in candidates if JobState(job.state) in states]
        if not candidates:
            raise ValueError("matching active job not found")
        if len(candidates) > 1:
            raise ValueError("multiple jobs match; specify a job id")
        return candidates[0]

    async def reconcile_startup(self, *, now: datetime | None = None) -> int:
        if self.recovery is None:
            return 0
        current = now or datetime.now(timezone.utc)

        cancelling = await self.jobs.list_states({JobState.CANCELLING.value})
        for job in cancelling:
            if not job.assigned_host or job.assigned_host == self.local_host_id:
                if self.sessions is not None:
                    await self.sessions.mark(job.id, SessionStatus.CANCELLED)
                await self._transition(job.id, JobState.CANCELLED)
                if self.recovery is not None:
                    await self.recovery.delete(job.id)
                await self.events.append(
                    "CANCEL_RECONCILED_LOCAL",
                    job_id=job.id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                )
                continue

            existing_cancel = await self.recovery.get(job.id)
            await self.recovery.upsert(
                job.id,
                kind=existing_cancel.kind if existing_cancel else RecoveryKind.RESTART.value,
                mode=RecoveryMode.CANCEL.value,
                attempt_count=existing_cancel.attempt_count if existing_cancel else 0,
                next_retry_at=None,
                execution_id=existing_cancel.execution_id if existing_cancel else None,
                resume_instruction=None,
                last_error=(
                    existing_cancel.last_error
                    if existing_cancel
                    else "controller restarted while cancellation was pending"
                ),
            )
            await self.events.append(
                "CANCEL_RECONCILE_WAIT",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={
                    "execution_id": existing_cancel.execution_id if existing_cancel else None
                },
            )

        preexisting_waiting_host = await self.jobs.list_states(
            {JobState.WAITING_HOST.value}
        )
        recoverable = await self.jobs.list_states(
            {
                JobState.ASSIGNED.value,
                JobState.STARTING.value,
                JobState.RUNNING.value,
            }
        )
        count = 0
        for job in recoverable:
            existing = await self.recovery.get(job.id)
            session_id = await self._external_session_id(job)
            mode = RecoveryMode.RESUME if session_id else RecoveryMode.START
            execution_id = existing.execution_id if existing is not None else None
            delay = (
                self.restart_grace_seconds
                if job.assigned_host and job.assigned_host != self.local_host_id
                else 0
            )
            if self.sessions is not None:
                await self.sessions.mark(job.id, SessionStatus.WAITING_HOST)
            await self._transition(job.id, JobState.WAITING_HOST)
            await self.recovery.upsert(
                job.id,
                kind=RecoveryKind.RESTART.value,
                mode=mode.value,
                attempt_count=existing.attempt_count if existing else 0,
                next_retry_at=current + timedelta(seconds=delay),
                execution_id=execution_id,
                resume_instruction=RESTART_RESUME_INSTRUCTION if mode == RecoveryMode.RESUME else None,
                last_error="controller restarted while job was active",
            )
            await self.events.append(
                "CONTROLLER_RECONCILE_WAIT",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={"execution_id": execution_id, "mode": mode.value},
            )
            count += 1

        waiting_agent = await self.jobs.list_states({JobState.WAITING_AGENT.value})
        for job in waiting_agent:
            work = await self.project_work_for(job.id)
            project = self.projects.get(job.project_id)
            adapter = (
                self.project_adapters.get(project)
                if self.project_adapters is not None
                else None
            )
            if (
                work is not None
                and adapter is not None
                and adapter.requires_submission
            ):
                if self.sessions is not None:
                    await self.sessions.mark(job.id, SessionStatus.WAITING_HOST)
                await self._transition(job.id, JobState.WAITING_HOST)
                await self.recovery.upsert(
                    job.id,
                    kind=RecoveryKind.RESTART.value,
                    mode=RecoveryMode.FINALIZE.value,
                    attempt_count=0,
                    next_retry_at=current,
                    execution_id=None,
                    resume_instruction=None,
                    last_error="controller restarted during project result finalization",
                )
                await self.events.append(
                    "PROJECT_FINALIZE_RECONCILE_WAIT",
                    job_id=job.id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={"task_id": work.task_id},
                )
                count += 1
                continue

            existing = await self.recovery.get(job.id)
            session_id = await self._external_session_id(job)
            mode = RecoveryMode.RESUME if session_id else RecoveryMode.START
            if self.sessions is not None:
                await self.sessions.mark(job.id, SessionStatus.WAITING_HOST)
            await self._transition(job.id, JobState.WAITING_HOST)
            await self.recovery.upsert(
                job.id,
                kind=RecoveryKind.RESTART.value,
                mode=mode.value,
                attempt_count=existing.attempt_count if existing else 0,
                next_retry_at=current,
                execution_id=None,
                resume_instruction=RESTART_RESUME_INSTRUCTION if mode == RecoveryMode.RESUME else None,
                last_error="controller restarted in WAITING_AGENT without adapter finalization",
            )
            count += 1

        waiting_quota = await self.jobs.list_states({JobState.WAITING_QUOTA.value})
        for job in waiting_quota:
            if await self.recovery.get(job.id) is None:
                await self.recovery.upsert(
                    job.id,
                    kind=RecoveryKind.QUOTA.value,
                    mode=RecoveryMode.RESUME.value,
                    attempt_count=1,
                    next_retry_at=current + timedelta(seconds=self.quota_retry_initial_seconds),
                    execution_id=None,
                    resume_instruction=QUOTA_RESUME_INSTRUCTION,
                    last_error="recovered WAITING_QUOTA without retry metadata",
                )

        for job in preexisting_waiting_host:
            existing = await self.recovery.get(job.id)
            if existing is None:
                await self.recovery.upsert(
                    job.id,
                    kind=RecoveryKind.HOST.value,
                    mode=(
                        RecoveryMode.RESUME.value
                        if await self._external_session_id(job)
                        else RecoveryMode.START.value
                    ),
                    attempt_count=0,
                    next_retry_at=current,
                    execution_id=None,
                    resume_instruction=(
                        RESTART_RESUME_INSTRUCTION
                        if await self._external_session_id(job)
                        else None
                    ),
                    last_error="recovered WAITING_HOST without recovery metadata",
                )
            else:
                await self.recovery.upsert(
                    job.id,
                    kind=existing.kind,
                    mode=existing.mode,
                    attempt_count=existing.attempt_count,
                    next_retry_at=current,
                    execution_id=existing.execution_id,
                    resume_instruction=existing.resume_instruction,
                    last_error=existing.last_error,
                )
                await self.events.append(
                    "WAITING_HOST_REARMED_ON_STARTUP",
                    job_id=job.id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "previous_retry_at": (
                            existing.next_retry_at.isoformat()
                            if existing.next_retry_at
                            else None
                        ),
                        "next_retry_at": current.isoformat(),
                    },
                )
        return count

    async def reconcile_runner(
        self,
        *,
        host_id: str,
        running_jobs: list[dict],
        completed_jobs: list[dict],
        gateway: RunnerGateway,
    ) -> int:
        if self.recovery is None:
            return 0
        reported = {
            str(item.get("execution_id")): item
            for item in [*running_jobs, *completed_jobs]
            if isinstance(item, dict) and item.get("execution_id")
        }
        if not reported:
            return 0

        waiting = await self.jobs.list_states({JobState.WAITING_HOST.value})
        adopted = 0
        for job in waiting:
            if job.assigned_host != host_id:
                continue
            record = await self.recovery.get(job.id)
            if record is None or not record.execution_id:
                continue
            report = reported.get(record.execution_id)
            if report is None:
                continue
            session_id = (
                str(report.get("session_id"))
                if report.get("session_id")
                else job.external_session_id
            )
            handle = gateway.adopt_remote(
                host_id=host_id,
                execution_id=record.execution_id,
                session_id=session_id,
                on_event=self._event_callback(job.id),
            )
            await self._transition(job.id, JobState.ASSIGNED, assigned_host=host_id)
            await self._transition(job.id, JobState.STARTING)
            await self._set_handle(job.id, handle)
            await self._transition(job.id, JobState.RUNNING)
            if self.sessions is not None:
                await self.sessions.mark(job.id, SessionStatus.ACTIVE)
            self._start_task(job.id, self._execute_adopted(job.id, handle))
            await self.events.append(
                "RUNNER_JOB_ADOPTED",
                job_id=job.id,
                project_id=job.project_id,
                host_id=host_id,
                payload={
                    "execution_id": record.execution_id,
                    "session_id": session_id,
                },
            )
            adopted += 1

        cancelling = await self.jobs.list_states({JobState.CANCELLING.value})
        for job in cancelling:
            if job.assigned_host != host_id:
                continue
            record = await self.recovery.get(job.id)
            if (
                record is None
                or record.mode != RecoveryMode.CANCEL.value
                or not record.execution_id
            ):
                continue
            report = reported.get(record.execution_id)
            if report is None:
                continue
            session_id = (
                str(report.get("session_id"))
                if report.get("session_id")
                else job.external_session_id
            )
            handle = gateway.adopt_remote(
                host_id=host_id,
                execution_id=record.execution_id,
                session_id=session_id,
                on_event=self._event_callback(job.id),
            )
            await self._set_handle(job.id, handle)
            self._start_task(job.id, self._execute_cancel_adopted(job.id, handle))
            await self.events.append(
                "RUNNER_CANCEL_ADOPTED",
                job_id=job.id,
                project_id=job.project_id,
                host_id=host_id,
                payload={
                    "execution_id": record.execution_id,
                    "session_id": session_id,
                },
            )
            adopted += 1
        return adopted

    async def expire_approvals(self, *, now: datetime | None = None) -> int:
        if self.approvals is None:
            return 0
        current = now or datetime.now(timezone.utc)
        count = 0
        for approval in await self.approvals.expired(current):
            await self.approvals.expire(approval.id)
            job = await self.require(approval.job_id)
            if JobState(job.state) != JobState.WAITING_HUMAN:
                continue
            await self.jobs.update(job.id, error="human approval expired")
            if self.sessions is not None:
                await self.sessions.mark(job.id, SessionStatus.FAILED)
            await self._transition(job.id, JobState.FAILED)
            await self.events.append(
                "HUMAN_GATE_EXPIRED",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={"approval_id": approval.id},
            )
            await self._notify(job.id, "⌛ Human Gate 승인 시간이 만료되어 작업을 종료했습니다.")
            count += 1
        return count

    async def recover_due(self, *, now: datetime | None = None) -> int:
        if self.recovery is None:
            return 0
        current = now or datetime.now(timezone.utc)
        recovered = 0
        for record in await self.recovery.list():
            job = await self.jobs.get(record.job_id)
            if job is None or JobState(job.state) in TERMINAL_STATES:
                await self.recovery.delete(record.job_id)
                continue
            if not _is_due(record.next_retry_at, current):
                continue
            state = JobState(job.state)
            if state == JobState.WAITING_QUOTA:
                if await self._retry_quota(job, record):
                    recovered += 1
            elif state == JobState.WAITING_HOST:
                if await self._retry_host(job, record):
                    recovered += 1
        return recovered

    async def _retry_quota(self, job: JobRecord, record: RecoveryRecord) -> bool:
        if job.assigned_host and self.hosts is not None:
            if not await self.hosts.is_online(job.assigned_host):
                return False
        await self._transition(job.id, JobState.RUNNING)
        if self.sessions is not None:
            await self.sessions.mark(job.id, SessionStatus.ACTIVE)
        await self.events.append(
            "QUOTA_RESUMED",
            job_id=job.id,
            project_id=job.project_id,
            host_id=job.assigned_host,
            payload={"attempt": record.attempt_count},
        )
        instruction = record.resume_instruction or QUOTA_RESUME_INSTRUCTION
        await self.recovery.upsert(
            job.id,
            kind=RecoveryKind.QUOTA.value,
            mode=RecoveryMode.RESUME.value,
            attempt_count=record.attempt_count,
            next_retry_at=None,
            execution_id=record.execution_id,
            resume_instruction=instruction,
            last_error=record.last_error,
        )
        self._start_task(job.id, self._execute_resume(job.id, instruction))
        await self._notify(job.id, "↻ Codex quota 대기 시간이 끝나 자동 재시도합니다.")
        return True

    async def _retry_host(self, job: JobRecord, record: RecoveryRecord) -> bool:
        work = await self.project_work_for(job.id)
        requested_host = (
            work.host_id
            if work is not None and work.task_id and work.host_id
            else (
                job.assigned_host
                if job.external_session_id and job.assigned_host
                else job.requested_host
            )
        )
        try:
            host_id = await self._resolve_host(job.project_id, requested_host)
        except HostUnavailable:
            return False

        await self._transition(job.id, JobState.ASSIGNED, assigned_host=host_id)
        mode = RecoveryMode(record.mode)
        if mode == RecoveryMode.START:
            await self.recovery.upsert(
                job.id,
                kind=RecoveryKind.HOST.value,
                mode=RecoveryMode.START.value,
                attempt_count=record.attempt_count,
                next_retry_at=None,
                execution_id=record.execution_id,
                resume_instruction=record.resume_instruction,
                last_error=record.last_error,
            )
            self._start_task(job.id, self._execute_new(job.id))
        elif mode == RecoveryMode.FINALIZE:
            await self._transition(job.id, JobState.STARTING)
            await self.recovery.upsert(
                job.id,
                kind=record.kind,
                mode=RecoveryMode.FINALIZE.value,
                attempt_count=record.attempt_count,
                next_retry_at=None,
                execution_id=None,
                resume_instruction=None,
                last_error=record.last_error,
            )
            self._start_task(job.id, self._execute_project_finalize(job.id))
        else:
            await self._transition(job.id, JobState.STARTING)
            await self._transition(job.id, JobState.RUNNING)
            if self.sessions is not None:
                await self.sessions.mark(job.id, SessionStatus.ACTIVE)
            instruction = record.resume_instruction or RESTART_RESUME_INSTRUCTION
            await self.recovery.upsert(
                job.id,
                kind=record.kind,
                mode=RecoveryMode.RESUME.value,
                attempt_count=record.attempt_count,
                next_retry_at=None,
                execution_id=record.execution_id,
                resume_instruction=instruction,
                last_error=record.last_error,
            )
            self._start_task(job.id, self._execute_resume(job.id, instruction))

        await self.events.append(
            "HOST_WAIT_RESUMED",
            job_id=job.id,
            project_id=job.project_id,
            host_id=host_id,
            payload={"mode": mode.value},
        )
        await self._notify(job.id, f"↻ Host {host_id}가 사용 가능해져 작업을 재개합니다.")
        return True

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

    def _validate_requested_host(self, project_id: str, requested_host: str) -> None:
        if requested_host == "auto":
            return
        project = self.projects.get(project_id)
        if requested_host not in project.allowed_hosts:
            raise ValueError(
                f"host {requested_host!r} is not allowed for project {project_id!r}"
            )
        project.path_for(requested_host)

    def _start_task(self, job_id: str, coroutine) -> None:
        task = asyncio.create_task(coroutine, name=f"job:{job_id}")
        self._tasks[job_id] = task

        def cleanup(done_task: asyncio.Task[None]) -> None:
            if self._tasks.get(job_id) is done_task:
                self._tasks.pop(job_id, None)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "Unhandled Job task exception job_id=%s",
                    job_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(cleanup)

    async def _execute_new(self, job_id: str) -> None:
        try:
            job = await self._transition(job_id, JobState.STARTING)
            instruction = job.instruction
            if self.project_adapters is not None:
                project = self.projects.get(job.project_id)
                assert job.assigned_host is not None
                try:
                    prepared = await self.project_adapters.get(project).prepare(
                        job_id=job.id,
                        project=project,
                        host_id=job.assigned_host,
                        base_instruction=job.instruction,
                    )
                except NoProjectWork as exc:
                    await self.jobs.update(job_id, result=str(exc), error=None)
                    await self._transition(job_id, JobState.COMPLETED)
                    if self.recovery is not None:
                        await self.recovery.delete(job_id)
                    await self._notify(job_id, f"✅ 실행 가능한 Project OS 작업이 없습니다.\n\n{exc}")
                    return
                instruction = prepared.instruction
                if prepared.task_id:
                    await self._notify(
                        job_id,
                        f"📌 Project OS Task: {prepared.task_id}\nRole: {prepared.role or '-'}",
                    )

            current = await self.require(job_id)
            if current.external_session_id:
                await self._transition(job_id, JobState.RUNNING)
                result = await self._resume_or_fallback(job_id, instruction)
                if result is None:
                    return
                await self._finish_or_continue(job_id, result)
                return

            handle = await self._start_new_turn(job, instruction)
            await self._set_handle(job_id, handle)
            current = await self.require(job_id)
            if JobState(current.state) != JobState.STARTING:
                await handle.cancel()
                return
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

    async def _execute_retry(self, job_id: str) -> None:
        try:
            await self._transition(job_id, JobState.STARTING)
            await self._transition(job_id, JobState.RUNNING)
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.ACTIVE)
            result = await self._resume_or_fallback(
                job_id,
                RETRY_RESUME_INSTRUCTION,
            )
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

    async def _execute_adopted(self, job_id: str, handle: RunHandle) -> None:
        try:
            result = await self._await_handle(job_id, handle)
            if result is not None:
                await self._finish_or_continue(job_id, result)
        except asyncio.CancelledError:
            await self._handle_cancelled_task(job_id)
        except Exception as exc:
            await self._fail(job_id, exc)
        finally:
            self._handles.pop(job_id, None)

    async def _execute_cancel_adopted(
        self,
        job_id: str,
        handle: RunHandle,
    ) -> None:
        try:
            await handle.cancel()
            await self._finalize_cancellation(job_id)
        except (ConnectionError, TimeoutError) as exc:
            await self._record_cancel_pending(job_id, handle, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_cancel_pending(job_id, handle, exc)
        finally:
            if self._handles.get(job_id) is handle:
                self._handles.pop(job_id, None)

    async def _execute_project_finalize(self, job_id: str) -> None:
        try:
            job = await self.require(job_id)
            await self._finalize_project_adapter(job_id, job.result)
        except asyncio.CancelledError:
            await self._handle_cancelled_task(job_id)
        except Exception as exc:
            await self._fail(job_id, exc)

    async def _finalize_project_adapter(
        self,
        job_id: str,
        final_message: str | None,
    ) -> None:
        current = await self.require(job_id)
        project = self.projects.get(current.project_id)
        if self.project_adapters is None:
            raise RuntimeError("project adapter registry is not enabled")
        if current.assigned_host is None:
            raise RuntimeError("project finalization requires an assigned host")
        adapter = self.project_adapters.get(project)
        if not adapter.requires_submission:
            raise RuntimeError(
                f"adapter {project.adapter!r} does not require result finalization"
            )

        try:
            outcome = await adapter.submit_result(
                job_id=job_id,
                project=project,
                host_id=current.assigned_host,
                final_message=final_message,
            )
        except ConnectionError as exc:
            if self.recovery is None:
                raise
            await self._enter_host_wait(
                job_id,
                mode=RecoveryMode.FINALIZE,
                error=str(exc),
                assigned_host=current.assigned_host,
            )
            return
        except Exception as exc:
            await self.jobs.update(job_id, error=f"Project OS submit failed: {exc}")
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.FAILED)
            await self._transition(job_id, JobState.FAILED)
            if self.recovery is not None:
                await self.recovery.delete(job_id)
            await self._notify(
                job_id,
                f"❌ 구현은 끝났지만 Project OS 결과 제출에 실패했습니다: {exc}",
            )
            return

        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.IDLE)
        await self._transition(job_id, JobState.COMPLETED)
        if self.recovery is not None:
            await self.recovery.delete(job_id)
        work = await self.project_work_for(job_id)
        task_text = f"\nTask: {work.task_id}" if work and work.task_id else ""
        next_text = (
            f"\nNext Task: {outcome.next_task_id}"
            if outcome.next_task_id
            else "\nNext Task: -"
        )
        await self._notify(
            job_id,
            "✅ Project OS 구현 handoff를 제출했습니다."
            + task_text
            + next_text
            + _optional_detail(final_message),
        )

    async def _finish_or_continue(
        self,
        job_id: str,
        result: AgentRunResult,
    ) -> None:
        current_result = result
        while True:
            current = await self.require(job_id)
            if JobState(current.state) in {
                JobState.WAITING_HUMAN,
                JobState.WAITING_HOST,
                JobState.WAITING_QUOTA,
            }:
                return

            if current_result.returncode != 0:
                quota = (
                    QuotaSignal(current_result.final_message or "quota unavailable", current_result.retry_at)
                    if current_result.retry_kind == "quota"
                    else detect_quota_text(current_result.final_message)
                )
                if quota is not None:
                    await self._enter_quota_wait(job_id, quota)
                    return
                if current_result.retry_kind == "host":
                    await self._enter_host_wait(
                        job_id,
                        mode=RecoveryMode.RESUME,
                        error=current_result.final_message or "runner unavailable",
                        assigned_host=current.assigned_host,
                        resume_instruction=RESTART_RESUME_INSTRUCTION,
                    )
                    return

                await self.jobs.update(job_id, error=current_result.final_message)
                if self.sessions is not None:
                    await self.sessions.mark(job_id, SessionStatus.FAILED)
                await self._transition(job_id, JobState.FAILED)
                if self.recovery is not None:
                    await self.recovery.delete(job_id)
                await self._notify(
                    job_id,
                    "❌ Agent 실행이 실패했습니다."
                    + _optional_detail(current_result.final_message),
                )
                return

            completed = False
            finalize_project_os = False
            async with self._job_locks[job_id]:
                current = await self.require(job_id)
                if JobState(current.state) in {
                    JobState.WAITING_HUMAN,
                    JobState.WAITING_HOST,
                    JobState.WAITING_QUOTA,
                }:
                    return
                steering = self._drain_steering(job_id)
                if steering is None:
                    project = self.projects.get(current.project_id)
                    adapter = (
                        self.project_adapters.get(project)
                        if self.project_adapters is not None
                        else None
                    )
                    if adapter is not None and adapter.requires_submission:
                        await self.jobs.update(
                            job_id,
                            result=current_result.final_message,
                            error=None,
                        )
                        await self._transition(job_id, JobState.WAITING_AGENT)
                        finalize_project_os = True
                    else:
                        await self.jobs.update(
                            job_id,
                            result=current_result.final_message,
                            error=None,
                        )
                        if self.sessions is not None:
                            await self.sessions.mark(job_id, SessionStatus.IDLE)
                        await self._transition(job_id, JobState.COMPLETED)
                        if self.recovery is not None:
                            await self.recovery.delete(job_id)
                        completed = True
                else:
                    job = await self.require(job_id)
                    await self.events.append(
                        "STEERING_APPLIED",
                        job_id=job_id,
                        project_id=job.project_id,
                        host_id=job.assigned_host,
                        payload={"instruction": steering},
                    )

            if finalize_project_os:
                await self._finalize_project_adapter(
                    job_id,
                    current_result.final_message,
                )
                return

            if completed:
                await self._notify(
                    job_id,
                    "✅ 작업이 완료되었습니다."
                    + _optional_detail(current_result.final_message),
                )
                return

            assert steering is not None
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
        requested_session_id = await self._external_session_id(job)

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
                    instruction=_with_control_protocol(instruction),
                    working_directory=working_directory,
                    host_id=job.assigned_host,
                    on_event=self._event_callback(job_id),
                )
                await self._set_handle(job_id, handle)
                current = await self.require(job_id)
                if JobState(current.state) != JobState.RUNNING:
                    await handle.cancel()
                    return None
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

                current = await self.require(job_id)
                if JobState(current.state) in {
                    JobState.WAITING_HUMAN,
                    JobState.WAITING_HOST,
                    JobState.WAITING_QUOTA,
                }:
                    return None

                fallback_allowed = _resume_failure_allows_fallback(
                    result.final_message
                )
                await self.events.append(
                    "SESSION_RESUME_FAILED",
                    job_id=job_id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "external_session_id": requested_session_id,
                        "returncode": result.returncode,
                        "error": result.final_message,
                        "fallback_allowed": fallback_allowed,
                    },
                )
                if not fallback_allowed:
                    raise RuntimeError(
                        "Codex session resume failed without safe fallback: "
                        + (result.final_message or f"returncode={result.returncode}")
                    )
            except ConnectionError as exc:
                await self._enter_host_wait(
                    job_id,
                    mode=RecoveryMode.RESUME,
                    error=str(exc),
                    assigned_host=job.assigned_host,
                    resume_instruction=instruction,
                )
                return None
            except Exception as exc:
                current = await self.require(job_id)
                if JobState(current.state) in {
                    JobState.WAITING_HUMAN,
                    JobState.WAITING_HOST,
                    JobState.WAITING_QUOTA,
                }:
                    return None
                quota = detect_quota_text(str(exc))
                if quota is not None:
                    await self._enter_quota_wait(job_id, quota, resume_instruction=instruction)
                    return None
                fallback_allowed = _resume_failure_allows_fallback(str(exc))
                await self.events.append(
                    "SESSION_RESUME_FAILED",
                    job_id=job_id,
                    project_id=job.project_id,
                    host_id=job.assigned_host,
                    payload={
                        "external_session_id": requested_session_id,
                        "error": str(exc),
                        "fallback_allowed": fallback_allowed,
                    },
                )
                if not fallback_allowed:
                    raise RuntimeError(
                        "Codex session resume failed without safe fallback: "
                        + str(exc)
                    ) from exc

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
        try:
            handle = await self._start_new_turn(job, fallback_instruction)
        except ConnectionError as exc:
            await self._enter_host_wait(
                job_id,
                mode=RecoveryMode.RESUME,
                error=str(exc),
                assigned_host=job.assigned_host,
                resume_instruction=instruction,
            )
            return None
        await self._set_handle(job_id, handle)
        current = await self.require(job_id)
        if JobState(current.state) != JobState.RUNNING:
            await handle.cancel()
            return None
        return await self._await_handle(job_id, handle)

    async def _start_new_turn(self, job: JobRecord, instruction: str) -> RunHandle:
        project = self.projects.get(job.project_id)
        assert job.assigned_host is not None
        working_directory = Path(project.path_for(job.assigned_host)).expanduser()
        return await self.runner.start(
            project_id=job.project_id,
            instruction=_with_control_protocol(instruction),
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

        if self.recovery is not None and handle.execution_id:
            existing = await self.recovery.get(job_id)
            await self.recovery.upsert(
                job_id,
                kind=existing.kind if existing else RecoveryKind.RESTART.value,
                mode=existing.mode if existing else RecoveryMode.ADOPT.value,
                attempt_count=existing.attempt_count if existing else 0,
                next_retry_at=None,
                execution_id=handle.execution_id,
                resume_instruction=existing.resume_instruction if existing else RESTART_RESUME_INSTRUCTION,
                last_error=existing.last_error if existing else None,
            )

    async def _await_handle(
        self,
        job_id: str,
        handle: RunHandle,
    ) -> AgentRunResult | None:
        try:
            result = await handle.wait()
        except asyncio.CancelledError:
            current = await self.require(job_id)
            if JobState(current.state) in {
                JobState.PAUSED,
                JobState.CANCELLING,
                JobState.CANCELLED,
                JobState.WAITING_HUMAN,
                JobState.WAITING_HOST,
                JobState.WAITING_QUOTA,
            }:
                return None
            raise
        except ConnectionError as exc:
            current = await self.require(job_id)
            if JobState(current.state) == JobState.CANCELLING:
                await self._record_cancel_pending(job_id, handle, exc)
                return None
            await self._enter_host_wait(
                job_id,
                mode=RecoveryMode.RESUME,
                error=str(exc),
                assigned_host=current.assigned_host,
                resume_instruction=RESTART_RESUME_INSTRUCTION,
            )
            return None

        current = await self.require(job_id)
        current_state = JobState(current.state)
        if current_state == JobState.CANCELLING:
            if result.retry_kind != "host":
                await self._finalize_cancellation(job_id, result=result)
            else:
                await self._record_cancel_pending(
                    job_id,
                    handle,
                    ConnectionError(result.final_message or "runner disconnected"),
                )
            return None
        if current_state in {
            JobState.PAUSED,
            JobState.CANCELLING,
            JobState.CANCELLED,
            JobState.WAITING_HUMAN,
            JobState.WAITING_HOST,
            JobState.WAITING_QUOTA,
        }:
            return None

        if result.session_id:
            await self._record_session(job_id, result.session_id)
        await self.jobs.update(
            job_id,
            external_session_id=result.session_id or current.external_session_id,
            result=result.final_message,
        )

        quota = (
            QuotaSignal(result.final_message or "quota unavailable", result.retry_at)
            if result.retry_kind == "quota"
            else (
                detect_quota_text(result.final_message)
                if result.returncode != 0
                else None
            )
        )
        if quota is not None:
            await self._enter_quota_wait(job_id, quota)
            return None
        if result.retry_kind == "host":
            await self._enter_host_wait(
                job_id,
                mode=RecoveryMode.RESUME,
                error=result.final_message or "runner unavailable",
                assigned_host=current.assigned_host,
                resume_instruction=RESTART_RESUME_INSTRUCTION,
            )
            return None

        current = await self.require(job_id)
        if (
            JobState(current.state) == JobState.RUNNING
            and result.final_message
            and (gate := extract_human_gate_from_text(result.final_message)) is not None
        ):
            await self._enter_human_gate(job_id, gate)
            return None

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

            quota = detect_quota_event(event)
            if quota is not None:
                await self._enter_quota_wait(job_id, quota)
                return

            gate = extract_human_gate(event)
            if gate is not None:
                await self._enter_human_gate(job_id, gate)
                return

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

    async def _enter_human_gate(
        self,
        job_id: str,
        request: HumanGateRequest,
    ) -> ApprovalRecord | None:
        if self.approvals is None:
            await self._notify(
                job_id,
                "⚠ Human Gate가 발생했지만 Approval Registry가 활성화되지 않았습니다.",
            )
            return None

        async with self._job_locks[job_id]:
            job = await self.require(job_id)
            state = JobState(job.state)
            if state == JobState.WAITING_HUMAN:
                return await self.approvals.pending_for_job(job_id)
            if state != JobState.RUNNING:
                return None

            approval = await self.approvals.create(
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                requested_by_user=job.requested_by_user,
                request=request,
            )
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.WAITING_HUMAN)
            await self._transition(job_id, JobState.WAITING_HUMAN)

        await self._notify_approval(job_id, self.approvals.prompt(approval))
        asyncio.create_task(
            self._stop_active_turn(job_id),
            name=f"human-gate-stop:{job_id}",
        )
        return approval

    async def _enter_quota_wait(
        self,
        job_id: str,
        signal: QuotaSignal,
        *,
        resume_instruction: str | None = None,
    ) -> RecoveryRecord | None:
        if self.recovery is None:
            return None
        async with self._job_locks[f"quota:{job_id}"]:
            job = await self.require(job_id)
            state = JobState(job.state)
            if state == JobState.WAITING_QUOTA:
                return await self.recovery.get(job_id)
            if state not in {JobState.STARTING, JobState.RUNNING}:
                return None

            existing = await self.recovery.get(job_id)
            attempt = (existing.attempt_count if existing else 0) + 1
            next_retry = retry_at(
                attempt_count=attempt,
                initial_seconds=self.quota_retry_initial_seconds,
                max_seconds=self.quota_retry_max_seconds,
                reset_at=signal.reset_at,
                reset_grace_seconds=self.quota_reset_grace_seconds,
            )
            execution_id = getattr(self._handles.get(job_id), "execution_id", None)
            record = await self.recovery.upsert(
                job_id,
                kind=RecoveryKind.QUOTA.value,
                mode=RecoveryMode.RESUME.value,
                attempt_count=attempt,
                next_retry_at=next_retry,
                execution_id=execution_id,
                resume_instruction=resume_instruction or QUOTA_RESUME_INSTRUCTION,
                last_error=signal.message,
            )
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.WAITING_QUOTA)
            await self._transition(job_id, JobState.WAITING_QUOTA)
            await self.events.append(
                "QUOTA_WAIT",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={
                    "attempt": attempt,
                    "next_retry_at": next_retry.isoformat(),
                    "reset_at": signal.reset_at.isoformat() if signal.reset_at else None,
                    "reset_grace_seconds": (
                        self.quota_reset_grace_seconds if signal.reset_at else 0
                    ),
                },
            )

        grace_text = (
            f" (Codex reset + {self.quota_reset_grace_seconds // 60}분 grace)"
            if signal.reset_at and self.quota_reset_grace_seconds
            else ""
        )
        await self._notify(
            job_id,
            "⏸ Codex 사용량 제한으로 대기합니다. "
            f"시도 {attempt}, 자동 재시도: {next_retry.isoformat()}{grace_text}",
        )
        asyncio.create_task(self._stop_active_turn(job_id), name=f"quota-stop:{job_id}")
        return record

    async def _enter_host_wait(
        self,
        job_id: str,
        *,
        mode: RecoveryMode,
        error: str,
        assigned_host: str | None,
        resume_instruction: str | None = None,
    ) -> RecoveryRecord | None:
        if self.recovery is None:
            raise ConnectionError(error)
        async with self._job_locks[f"host:{job_id}"]:
            job = await self.require(job_id)
            state = JobState(job.state)
            if state in TERMINAL_STATES:
                return None
            existing = await self.recovery.get(job_id)
            execution_id = (
                existing.execution_id
                if existing is not None
                else getattr(self._handles.get(job_id), "execution_id", None)
            )
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.WAITING_HOST)
            if state != JobState.WAITING_HOST:
                await self._transition(
                    job_id,
                    JobState.WAITING_HOST,
                    assigned_host=assigned_host or job.assigned_host,
                )
            record = await self.recovery.upsert(
                job_id,
                kind=RecoveryKind.HOST.value,
                mode=mode.value,
                attempt_count=existing.attempt_count if existing else 0,
                next_retry_at=datetime.now(timezone.utc),
                execution_id=execution_id,
                resume_instruction=resume_instruction,
                last_error=error,
            )
            await self.events.append(
                "HOST_WAIT",
                job_id=job.id,
                project_id=job.project_id,
                host_id=assigned_host or job.assigned_host,
                payload={"mode": mode.value, "error": error},
            )
        await self._notify(job_id, "⏸ 실행 Host가 오프라인이라 자동 재연결을 기다립니다.")
        return record

    async def _stop_active_turn(self, job_id: str) -> None:
        for _ in range(10):
            handle = self._handles.get(job_id)
            if handle is not None:
                await handle.cancel()
                return
            await asyncio.sleep(0)

    async def _record_session(self, job_id: str, external_session_id: str) -> None:
        job = await self.require(job_id)
        if job.assigned_host is None:
            return
        if job.external_session_id != external_session_id:
            await self.jobs.update(job_id, external_session_id=external_session_id)
        if self.sessions is not None:
            state = JobState(job.state)
            statuses = {
                JobState.WAITING_HUMAN: SessionStatus.WAITING_HUMAN,
                JobState.WAITING_HOST: SessionStatus.WAITING_HOST,
                JobState.WAITING_QUOTA: SessionStatus.WAITING_QUOTA,
                JobState.PAUSED: SessionStatus.PAUSED,
                JobState.CANCELLING: SessionStatus.CANCELLING,
            }
            await self.sessions.record(
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                external_session_id=external_session_id,
                status=statuses.get(state, SessionStatus.ACTIVE),
            )
        if self.project_sessions is not None:
            await self.project_sessions.bind_for_job(
                project_id=job.project_id,
                owner_user_id=job.requested_by_user,
                job_id=job.id,
                external_session_id=external_session_id,
                host_id=job.assigned_host,
            )

    async def _external_session_id(self, job: JobRecord) -> str | None:
        if self.sessions is not None:
            session = await self.sessions.get_for_job(job.id)
            if session is not None:
                return session.external_session_id
        return job.external_session_id

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

    async def _persist_cancel_intent(
        self,
        job_id: str,
        handle: RunHandle,
    ) -> None:
        execution_id = getattr(handle, "execution_id", None)
        if self.recovery is None or not execution_id:
            return
        existing = await self.recovery.get(job_id)
        await self.recovery.upsert(
            job_id,
            kind=existing.kind if existing else RecoveryKind.HOST.value,
            mode=RecoveryMode.CANCEL.value,
            attempt_count=existing.attempt_count if existing else 0,
            next_retry_at=None,
            execution_id=execution_id,
            resume_instruction=None,
            last_error=existing.last_error if existing else None,
        )

    async def _record_cancel_pending(
        self,
        job_id: str,
        handle: RunHandle,
        exc: Exception,
    ) -> None:
        execution_id = getattr(handle, "execution_id", None)
        if self.recovery is not None and execution_id:
            existing = await self.recovery.get(job_id)
            await self.recovery.upsert(
                job_id,
                kind=existing.kind if existing else RecoveryKind.HOST.value,
                mode=RecoveryMode.CANCEL.value,
                attempt_count=existing.attempt_count if existing else 0,
                next_retry_at=None,
                execution_id=execution_id,
                resume_instruction=None,
                last_error=str(exc),
            )
        job = await self.require(job_id)
        await self.events.append(
            "CANCEL_PENDING",
            job_id=job_id,
            project_id=job.project_id,
            host_id=job.assigned_host,
            payload={
                "execution_id": execution_id,
                "error": str(exc),
            },
        )

    async def _finalize_cancellation(
        self,
        job_id: str,
        *,
        result: AgentRunResult | None = None,
    ) -> None:
        async with self._job_locks[f"cancel-finalize:{job_id}"]:
            job = await self.require(job_id)
            state = JobState(job.state)
            if state == JobState.CANCELLED:
                return
            if state != JobState.CANCELLING:
                return

            if result is not None:
                await self.jobs.update(
                    job_id,
                    result=result.final_message,
                    error=None,
                    external_session_id=result.session_id or job.external_session_id,
                )
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.CANCELLED)
            if self.recovery is not None:
                await self.recovery.delete(job_id)
            await self._transition(job_id, JobState.CANCELLED)
            await self.events.append(
                "CANCEL_CONFIRMED",
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={
                    "returncode": result.returncode if result is not None else None,
                    "execution_id": getattr(self._handles.get(job_id), "execution_id", None),
                },
            )

    async def _handle_cancelled_task(self, job_id: str) -> None:
        current = await self.require(job_id)
        state = JobState(current.state)
        if state in {
            JobState.PAUSED,
            JobState.CANCELLING,
            JobState.CANCELLED,
            JobState.WAITING_HUMAN,
            JobState.WAITING_HOST,
            JobState.WAITING_QUOTA,
        }:
            return
        if state not in TERMINAL_STATES:
            await self._transition(job_id, JobState.CANCELLED)
            if self.recovery is not None:
                await self.recovery.delete(job_id)

    async def _fail(self, job_id: str, exc: Exception) -> None:
        current = await self.require(job_id)
        state = JobState(current.state)
        if state in TERMINAL_STATES or state in {
            JobState.PAUSED,
            JobState.CANCELLING,
            JobState.WAITING_HUMAN,
            JobState.WAITING_HOST,
            JobState.WAITING_QUOTA,
        }:
            return
        if isinstance(exc, ConnectionError):
            await self._enter_host_wait(
                job_id,
                mode=RecoveryMode.RESUME if current.external_session_id else RecoveryMode.START,
                error=str(exc),
                assigned_host=current.assigned_host,
                resume_instruction=RESTART_RESUME_INSTRUCTION,
            )
            return
        quota = detect_quota_text(str(exc))
        if quota is not None:
            await self._enter_quota_wait(job_id, quota)
            return
        await self.jobs.update(job_id, error=str(exc))
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.FAILED)
        await self._transition(job_id, JobState.FAILED)
        if self.recovery is not None:
            await self.recovery.delete(job_id)
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
        if target in TERMINAL_STATES and self.project_sessions is not None:
            await self.project_sessions.release_for_job(
                project_id=job.project_id,
                owner_user_id=job.requested_by_user,
                job_id=job.id,
            )
        return updated

    async def _notify(self, job_id: str, message: str) -> None:
        job = await self.require(job_id)
        notifier = self._notifiers.get(job.requested_by_channel)
        if notifier is None:
            return
        work = await self.project_work_for(job_id)
        scope = [job.project_id]
        if work is not None and work.task_id:
            scope.append(work.task_id)
        scope.append(job.id)
        text = f"[{' / '.join(scope)}]\n{message}"

        async def send() -> None:
            await notifier(job.requested_by_user, text)

        await self._deliver_notification(
            job,
            notification_type="message",
            send=send,
        )

    async def _notify_approval(self, job_id: str, approval: ApprovalPrompt) -> None:
        job = await self.require(job_id)
        approval_notifier = self._approval_notifiers.get(job.requested_by_channel)
        if approval_notifier is not None:
            async def send_approval() -> None:
                await approval_notifier(job.requested_by_user, approval)

            await self._deliver_notification(
                job,
                notification_type="approval",
                send=send_approval,
            )
            return

        options = "\n".join(
            f"{option.key}. {option.label}" for option in approval.options
        )
        await self._notify(
            job_id,
            f"⚠ Human Gate\n\n{approval.question}\n\n{options}",
        )

    async def _deliver_notification(
        self,
        job: JobRecord,
        *,
        notification_type: str,
        send: Callable[[], Awaitable[None]],
    ) -> bool:
        attempts = 1 + len(_NOTIFICATION_RETRY_DELAYS)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                await send()
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = _NOTIFICATION_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "Notification delivery failed; retrying job_id=%s channel=%s "
                    "type=%s attempt=%s/%s delay=%.1fs error=%s",
                    job.id,
                    job.requested_by_channel,
                    notification_type,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        logger.error(
            "Notification delivery failed permanently job_id=%s channel=%s "
            "type=%s attempts=%s error=%s",
            job.id,
            job.requested_by_channel,
            notification_type,
            attempts,
            last_error,
            exc_info=(type(last_error), last_error, last_error.__traceback__),
        )
        try:
            await self.events.append(
                "NOTIFICATION_FAILED",
                job_id=job.id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                payload={
                    "channel": job.requested_by_channel,
                    "notification_type": notification_type,
                    "attempts": attempts,
                    "error_type": type(last_error).__name__,
                    "error": str(last_error)[:1000],
                },
            )
        except Exception:
            logger.exception(
                "Failed to persist NOTIFICATION_FAILED event job_id=%s",
                job.id,
            )
        return False


def _with_control_protocol(instruction: str) -> str:
    return f"{instruction.rstrip()}\n\n---\n\n{human_gate_protocol_instruction()}\n"


def _approval_instruction(
    prompt: ApprovalPrompt,
    *,
    option_key: str | None,
    rejected: bool,
    response_text: str | None,
) -> str:
    if rejected:
        decision = (
            "Human decision:\n"
            "The approval request was rejected. Do not perform the gated change. "
            "Continue with a safe alternative if one exists; otherwise explain the blocker."
        )
    else:
        option = next(
            (item for item in prompt.options if item.key == option_key),
            None,
        )
        label = option.label if option is not None else option_key or "selected option"
        decision = (
            "Human decision:\n"
            f"Proceed with option {option_key}: {label}."
        )

    if response_text:
        decision += f"\nAdditional human note:\n{response_text.strip()}"
    decision += f"\n\nOriginal question:\n{prompt.question}"
    return decision


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
        "question",
        "prompt",
        "details",
        "description",
        "header",
        "approval_type",
        "resets_at",
        "reset_at",
        "retry_at",
        "retry_after",
        "retry_after_seconds",
    ):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[key] = value

    options = event.get("options")
    if isinstance(options, list):
        allowed["options"] = options[:8]

    item = event.get("item")
    if isinstance(item, dict):
        clean_item = {}
        for key in (
            "type",
            "text",
            "content",
            "command",
            "status",
            "exit_code",
            "question",
            "prompt",
            "details",
            "description",
            "header",
            "approval_type",
        ):
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean_item[key] = value
        item_options = item.get("options")
        if isinstance(item_options, list):
            clean_item["options"] = item_options[:8]
        allowed["item"] = clean_item
    return allowed


def _resume_failure_allows_fallback(message: str | None) -> bool:
    if not message:
        return False
    normalized = " ".join(message.lower().split())
    if "session_identity_mismatch" in normalized:
        return True
    explicit_absence_markers = (
        "no saved session",
        "no session found",
        "session not found",
        "thread not found",
        "no saved thread",
        "rollout not found",
        "no rollout found",
        "could not find session",
        "couldn't find session",
        "failed to find session",
        "could not find thread",
        "failed to find thread",
    )
    if any(marker in normalized for marker in explicit_absence_markers):
        return True
    return (
        ("session " in normalized or "thread " in normalized or "rollout " in normalized)
        and " not found" in normalized
    )


def _optional_detail(message: str | None) -> str:
    if not message:
        return ""
    text = message.strip()
    if not text:
        return ""
    if len(text) > 1500:
        text = text[:1499] + "…"
    return f"\n\n{text}"


def _is_due(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now
