from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from remote_control.approvals.registry import ApprovalPrompt, ApprovalRegistry
from remote_control.controller.states import JobState, TERMINAL_STATES, validate_transition
from remote_control.feedback import FeedbackPolicy, FeedbackThrottler
from remote_control.hosts.registry import HostRegistry
from remote_control.hosts.router import HostRouter
from remote_control.human_gate import (
    HumanGateRequest,
    extract_human_gate,
    extract_human_gate_from_text,
    human_gate_protocol_instruction,
)
from remote_control.projects.registry import ProjectRegistry
from remote_control.runners.base import AgentRunResult, AgentRunner, RunHandle
from remote_control.runners.codex import extract_session_id
from remote_control.sessions.registry import SessionRegistry, SessionStatus
from remote_control.storage.models import ApprovalRecord, JobRecord
from remote_control.storage.repositories import EventRepository, JobRepository

Notifier = Callable[[str, str], Awaitable[None]]
ApprovalNotifier = Callable[[str, ApprovalPrompt], Awaitable[None]]

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
        approvals: ApprovalRegistry | None = None,
        progress_interval_seconds: int = 300,
    ) -> None:
        self.projects = projects
        self.jobs = jobs
        self.events = events
        self.runner = runner
        self.local_host_id = local_host_id
        self.hosts = hosts
        self.sessions = sessions
        self.approvals = approvals
        self.host_router = HostRouter(hosts) if hosts is not None else None
        self.feedback_policy = FeedbackPolicy()
        self.feedback_throttler = FeedbackThrottler(progress_interval_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._handles: dict[str, RunHandle] = {}
        self._steering: dict[str, list[str]] = defaultdict(list)
        self._job_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._notifier: Notifier | None = None
        self._approval_notifier: ApprovalNotifier | None = None

    def set_notifier(self, notifier: Notifier | None) -> None:
        self._notifier = notifier

    def set_approval_notifier(self, notifier: ApprovalNotifier | None) -> None:
        self._approval_notifier = notifier

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
        job = await self.require(job_id)
        current = JobState(job.state)
        if current in TERMINAL_STATES:
            return job

        await self._transition(job_id, JobState.CANCELLED)
        if self.sessions is not None:
            await self.sessions.mark(job_id, SessionStatus.CANCELLED)
        if self.approvals is not None:
            await self.approvals.cancel_for_job(job_id)

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

    async def pending_approvals_for_user(self, user_id: str) -> list[ApprovalRecord]:
        if self.approvals is None:
            return []
        return await self.approvals.pending_for_user(user_id)

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
    ) -> ApprovalRecord | None:
        if self.approvals is None:
            return None
        pending = await self.approvals.pending_for_user(user_id)
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

        if job.assigned_host and self.hosts is not None:
            if not await self.hosts.is_online(job.assigned_host):
                raise ValueError(
                    f"host {job.assigned_host!r} is offline; WAITING_HOST recovery is Phase R4"
                )

        await self._transition(job.id, JobState.RUNNING)
        if self.sessions is not None:
            await self.sessions.mark(job.id, SessionStatus.ACTIVE)

        instruction = _approval_instruction(
            prompt,
            option_key=resolved.selected_option,
            rejected=rejected,
            response_text=response_text,
        )
        self._start_task(job.id, self._execute_resume(job.id, instruction))
        return resolved

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
            current = await self.require(job_id)
            if JobState(current.state) == JobState.WAITING_HUMAN:
                return

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

            completed = False
            async with self._job_locks[job_id]:
                current = await self.require(job_id)
                if JobState(current.state) == JobState.WAITING_HUMAN:
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
                    instruction=_with_control_protocol(instruction),
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

                current = await self.require(job_id)
                if JobState(current.state) == JobState.WAITING_HUMAN:
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
                current = await self.require(job_id)
                if JobState(current.state) == JobState.WAITING_HUMAN:
                    return None
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
                JobState.CANCELLED,
                JobState.WAITING_HUMAN,
            }:
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

        current = await self.require(job_id)
        if (
            JobState(current.state) == JobState.RUNNING
            and result.final_message
            and (gate := extract_human_gate_from_text(result.final_message)) is not None
        ):
            await self._enter_human_gate(job_id, gate)

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
            await self._transition(job_id, JobState.WAITING_HUMAN)
            if self.sessions is not None:
                await self.sessions.mark(job_id, SessionStatus.WAITING_HUMAN)

        await self._notify_approval(job_id, self.approvals.prompt(approval))
        asyncio.create_task(
            self._stop_active_turn_for_human_gate(job_id),
            name=f"human-gate-stop:{job_id}",
        )
        return approval

    async def _stop_active_turn_for_human_gate(self, job_id: str) -> None:
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
            status = (
                SessionStatus.WAITING_HUMAN
                if JobState(job.state) == JobState.WAITING_HUMAN
                else SessionStatus.ACTIVE
            )
            await self.sessions.record(
                job_id=job_id,
                project_id=job.project_id,
                host_id=job.assigned_host,
                external_session_id=external_session_id,
                status=status,
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
        if state in {
            JobState.PAUSED,
            JobState.CANCELLED,
            JobState.WAITING_HUMAN,
        }:
            return
        if state not in TERMINAL_STATES:
            await self._transition(job_id, JobState.CANCELLED)

    async def _fail(self, job_id: str, exc: Exception) -> None:
        current = await self.require(job_id)
        state = JobState(current.state)
        if state in TERMINAL_STATES or state in {
            JobState.PAUSED,
            JobState.WAITING_HUMAN,
        }:
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

    async def _notify_approval(self, job_id: str, approval: ApprovalPrompt) -> None:
        job = await self.require(job_id)
        if job.requested_by_channel != "telegram":
            return
        if self._approval_notifier is not None:
            await self._approval_notifier(job.requested_by_user, approval)
            return

        options = "\n".join(
            f"{option.key}. {option.label}" for option in approval.options
        )
        await self._notify(
            job_id,
            f"⚠ Human Gate\n\n{approval.question}\n\n{options}",
        )


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


def _optional_detail(message: str | None) -> str:
    if not message:
        return ""
    text = message.strip()
    if not text:
        return ""
    if len(text) > 1500:
        text = text[:1499] + "…"
    return f"\n\n{text}"
