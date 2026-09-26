from __future__ import annotations

from collections.abc import Callable

from remote_control.controller.command_router import Command, CommandRouter, Intent
from remote_control.controller.job_manager import JobManager
from remote_control.controller.states import JobState
from remote_control.hosts.registry import HostRegistry
from remote_control.projects.registry import ProjectRegistry
from remote_control.storage.models import RecoveryRecord


DEFAULT_INSTRUCTION = (
    "Continue the next appropriate implementation task for this project. "
    "First inspect the repository state and project instructions. "
    "Make the smallest coherent change, run relevant tests, and summarize the result. "
    "Do not modify files outside this repository."
)

_STEERABLE_STATES = {
    JobState.ASSIGNED,
    JobState.STARTING,
    JobState.RUNNING,
}


class ControllerService:
    def __init__(
        self,
        *,
        projects: ProjectRegistry,
        jobs: JobManager,
        hosts: HostRegistry | None = None,
        diagnostics: Callable[[], str] | None = None,
    ) -> None:
        self.projects = projects
        self.jobs = jobs
        self.hosts = hosts
        self.diagnostics = diagnostics
        self.router = CommandRouter(projects)

    async def handle_text(
        self,
        text: str,
        *,
        channel: str,
        user_id: str,
        project_id: str | None = None,
    ) -> str:
        stripped = text.strip()
        if stripped and not stripped.startswith("/"):
            pending = await self.jobs.pending_approvals_for_user(
                user_id,
                project_id=project_id,
            )
            if len(pending) == 1:
                resolved = await self.jobs.match_pending_approval(
                    user_id,
                    stripped,
                    project_id=project_id,
                )
                if resolved is not None:
                    return _approval_response_text(
                        resolved.id,
                        resolved.selected_option,
                        False,
                    )
                assert self.jobs.approvals is not None
                options = ", ".join(
                    option.key for option in self.jobs.approvals.options(pending[0])
                )
                return (
                    "⚠ Human Gate 응답 대기 중입니다. "
                    f"선택지({options}) 또는 Telegram 버튼을 사용하세요."
                )

            if project_id is not None:
                active = await self.jobs.active_for_user(
                    user_id,
                    project_id=project_id,
                )
                steerable = [
                    job
                    for job in active
                    if JobState(job.state) in _STEERABLE_STATES
                ]
                if len(steerable) > 1:
                    raise ValueError(
                        "multiple jobs match; reply to a Job message or select a Job"
                    )
                if len(steerable) == 1:
                    job = steerable[0]
                    await self.jobs.steer(job.id, stripped)
                    return (
                        f"↪ {job.id} 추가 지시 접수\n"
                        "현재 project topic의 Codex session에 적용합니다."
                    )
                if active:
                    states = ", ".join(sorted({job.state for job in active}))
                    return (
                        "현재 project에 추가 지시 가능한 Job이 없습니다. "
                        f"active state: {states}. /status 를 확인하세요."
                    )
                job = await self.jobs.create(
                    project_id=project_id,
                    instruction=stripped,
                    requested_by_channel=channel,
                    requested_by_user=user_id,
                )
                return _job_started_text(job)

        routed_text = text
        if project_id is not None and _is_bare_run(stripped):
            routed_text = f"/run {project_id}"

        command = self.router.parse(routed_text)
        return await self.execute(
            command,
            channel=channel,
            user_id=user_id,
            project_id=project_id,
        )

    async def approval_details(self, approval_id: str, *, user_id: str):
        return await self.jobs.approval_details(approval_id, user_id=user_id)

    async def respond_approval(
        self,
        approval_id: str,
        *,
        user_id: str,
        option_key: str | None = None,
        rejected: bool = False,
        response_text: str | None = None,
    ) -> str:
        record = await self.jobs.respond_approval(
            approval_id,
            user_id=user_id,
            option_key=option_key,
            rejected=rejected,
            response_text=response_text,
        )
        return _approval_response_text(
            record.id,
            record.selected_option,
            rejected,
        )

    async def execute(
        self,
        command: Command,
        *,
        channel: str,
        user_id: str,
        project_id: str | None = None,
    ) -> str:
        if command.intent == Intent.HELP:
            if project_id is not None:
                return (
                    f"Remote Agent Control — {project_id}\n"
                    "/run — 이 프로젝트 작업 시작\n"
                    "/status — 이 프로젝트 active Job\n"
                    "/queue — 실행 중/대기 Queue 관리\n"
                    "/jobs — 이 프로젝트 최근 Job\n"
                    "/job <job-id>\n/retry <failed-or-waiting-job-id>\n"
                    "/sessions\n/session — 현재 Project Session\n"
                    "/session new — 새 Session\n/session use <session-id> — 과거 Session 재사용\n"
                    "/pause [job-id]\n/resume [job-id]\n"
                    "/steer [--job <job-id>] <instruction>\n"
                    "/redirect [--job <job-id>] <instruction>\n/stop [job-id]\n"
                    "일반 메시지 — active Job 1개면 추가 지시, 없으면 새 Job"
                )
            return (
                "Remote Agent Control\n"
                "/projects\n/status\n/queue\n/hosts\n/run <project> [--host <host-id>]\n"
                "/jobs\n/job <job-id>\n/retry <failed-or-waiting-job-id>\n"
                "/sessions\n/session <session-id>\n/session use <session-id>\n/doctor\n"
                "/pause [job-id]\n/resume [job-id]\n"
                "/steer [--job <job-id>] <instruction>\n"
                "/redirect [--job <job-id>] <instruction>\n/stop [job-id]"
            )
        if command.intent == Intent.PROJECTS:
            projects = self.projects.list()
            if not projects:
                return "등록된 프로젝트가 없습니다."
            return "\n".join(
                f"{project.id} — {project.name} [{project.adapter}]"
                for project in projects
            )
        if command.intent == Intent.HOSTS:
            if self.hosts is None:
                return "Host Registry가 활성화되지 않았습니다."
            hosts = await self.hosts.list()
            if not hosts:
                return "등록된 Host가 없습니다."
            return "\n".join(
                f"{host.id} {host.status.value} os={host.os} "
                f"capabilities={','.join(sorted(host.capabilities)) or '-'}"
                for host in hosts
            )
        if command.intent == Intent.DOCTOR:
            if self.diagnostics is None:
                return "Runtime doctor가 활성화되지 않았습니다."
            return self.diagnostics()
        if command.intent == Intent.STATUS:
            active = await self.jobs.active_for_user(
                user_id,
                project_id=project_id,
            )
            if not active:
                return (
                    "현재 project의 active Job이 없습니다."
                    if project_id is not None
                    else "현재 active Job이 없습니다."
                )
            lines = []
            for job in active:
                recovery = await self.jobs.recovery_for(job.id)
                lines.append(
                    f"{job.id} {job.project_id} {job.state} "
                    f"host={job.assigned_host or '-'}{_recovery_suffix(recovery)}"
                )
            return "\n".join(lines)
        if command.intent == Intent.QUEUE:
            active = await self.jobs.active_for_user(
                user_id,
                project_id=project_id,
            )
            queued = await self.jobs.queued_for_user(
                user_id,
                project_id=project_id,
            )
            current = [
                job for job in active
                if JobState(job.state) != JobState.WAITING_LEASE
            ]
            if not current and not queued:
                return (
                    "현재 project의 실행/대기 Job이 없습니다."
                    if project_id is not None
                    else "현재 실행/대기 Job이 없습니다."
                )

            title = (
                f"🎛 {project_id} 작업 현황"
                if project_id is not None
                else "🎛 작업 현황"
            )
            lines = [title]
            if current:
                lines.append("\n현재")
                for job in sorted(current, key=lambda item: item.created_at):
                    lines.append(
                        f"• {job.id} {job.state} host={job.assigned_host or '-'}"
                    )
            else:
                lines.append("\n현재\n• 실행 중인 Job 없음")

            lines.append("\n대기열")
            if not queued:
                lines.append("• WAITING_LEASE Job 없음")
            else:
                for index, job in enumerate(queued, start=1):
                    instruction = " ".join(job.instruction.split())
                    if len(instruction) > 70:
                        instruction = instruction[:67] + "..."
                    lines.append(
                        f"{index}. {job.id} host={job.assigned_host or '-'}\n"
                        f"   {instruction}"
                    )
            return "\n".join(lines)
        if command.intent == Intent.RUN_PROJECT:
            assert command.project_id is not None
            if project_id is not None and command.project_id != project_id:
                raise ValueError(
                    "project topic에서는 해당 project만 실행할 수 있습니다"
                )
            job = await self.jobs.create(
                project_id=command.project_id,
                instruction=DEFAULT_INSTRUCTION,
                requested_by_channel=channel,
                requested_by_user=user_id,
                requested_host=command.host,
            )
            return _job_started_text(job)
        if command.intent == Intent.JOBS:
            jobs = await self.jobs.list(limit=50 if project_id is not None else 20)
            if project_id is not None:
                jobs = [job for job in jobs if job.project_id == project_id][:20]
            if not jobs:
                return (
                    "이 project의 Job이 없습니다."
                    if project_id is not None
                    else "Job이 없습니다."
                )
            lines = []
            for job in jobs:
                recovery = await self.jobs.recovery_for(job.id)
                lines.append(
                    f"{job.id} {job.project_id} {job.state} "
                    f"host={job.assigned_host or '-'}{_recovery_suffix(recovery)}"
                )
            return "\n".join(lines)
        if command.intent == Intent.SESSIONS:
            if self.jobs.project_sessions is None:
                return "Project Session Registry가 활성화되지 않았습니다."
            sessions = await self.jobs.project_sessions.list_for_user(
                user_id,
                project_id=project_id,
                limit=20,
            )
            if not sessions:
                return (
                    "이 project의 Project Session이 없습니다."
                    if project_id is not None
                    else "Project Session이 없습니다."
                )
            return "\n".join(
                f"{session.id} {session.project_id} {session.status} "
                f"codex={session.external_session_id or '-'} "
                f"last_job={session.last_job_id or '-'} "
                f"last={session.last_active_at.isoformat()}"
                for session in sessions
            )
        if command.intent == Intent.NEW_SESSION:
            if self.jobs.project_sessions is None:
                return "Project Session Registry가 활성화되지 않았습니다."
            if project_id is None:
                raise ValueError("/session new 는 project topic에서 실행하세요")
            session = await self.jobs.project_sessions.new_session(
                project_id=project_id,
                owner_user_id=user_id,
            )
            return (
                f"🆕 새 Project Session을 만들었습니다.\n"
                f"Session: {session.id}\n"
                f"Project: {session.project_id}\n"
                "다음 일반 메시지부터 새 Codex thread를 사용합니다."
            )
        if command.intent == Intent.USE_SESSION:
            if self.jobs.project_sessions is None:
                return "Project Session Registry가 활성화되지 않았습니다."
            if project_id is None:
                raise ValueError("/session use 는 project topic에서 실행하세요")
            assert command.job_id is not None
            session = await self.jobs.project_sessions.use_session(
                session_id=command.job_id,
                project_id=project_id,
                owner_user_id=user_id,
            )
            return (
                f"↩ 이전 Project Session을 다시 활성화했습니다.\n"
                f"Session: {session.id}\n"
                f"Project: {session.project_id}\n"
                f"Codex session: {session.external_session_id}\n"
                f"Host: {session.host_id or '-'}\n"
                "다음 새 Job부터 이 Codex thread를 이어서 사용합니다."
            )
        if command.intent == Intent.SESSION:
            if self.jobs.project_sessions is None:
                return "Project Session Registry가 활성화되지 않았습니다."
            if command.job_id is None:
                if project_id is None:
                    raise ValueError(
                        "project topic에서는 /session, 그 외에서는 /session <session-id>를 사용하세요"
                    )
                session = await self.jobs.project_sessions.active_for(
                    project_id,
                    user_id,
                )
                if session is None:
                    return "현재 project의 active Project Session이 없습니다."
            else:
                session = await self.jobs.project_sessions.get(command.job_id)
                if session is None:
                    raise KeyError(f"unknown project session: {command.job_id}")
                if session.owner_user_id != user_id:
                    raise ValueError("project session belongs to another user")
                if project_id is not None and session.project_id != project_id:
                    raise ValueError("project session belongs to another project topic")
            return (
                f"{session.id}\n"
                f"Project: {session.project_id}\n"
                f"State: {session.status}\n"
                f"Codex session: {session.external_session_id or '-'}\n"
                f"Host: {session.host_id or '-'}\n"
                f"Last Job: {session.last_job_id or '-'}\n"
                f"Lock: {session.locked_by_job_id or '-'}\n"
                f"Created: {session.created_at.isoformat()}\n"
                f"Last active: {session.last_active_at.isoformat()}"
            )
        if command.intent == Intent.JOB:
            assert command.job_id is not None
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                project_id=project_id,
            )
            work = await self.jobs.project_work_for(job.id)
            recovery = await self.jobs.recovery_for(job.id)
            task_line = f"\nTask: {work.task_id}" if work and work.task_id else ""
            adapter_line = f"\nAdapter: {work.adapter}" if work else ""
            return (
                f"{job.id}\nProject: {job.project_id}\nState: {job.state}\n"
                f"Host: {job.assigned_host or '-'}\nSession: {job.external_session_id or '-'}"
                f"{adapter_line}{task_line}"
                f"\nError: {job.error or '-'}"
                f"{_recovery_detail(recovery)}"
            )
        if command.intent == Intent.RETRY:
            assert command.job_id is not None
            original = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                project_id=project_id,
            )
            state = JobState(original.state)
            if state == JobState.FAILED:
                retried = await self.jobs.retry_failed(original.id)
                return (
                    f"↻ 실패 Job 재시도 생성\n"
                    f"Retry of: {original.id}\n"
                    f"Job: {retried.id}\n"
                    f"Project: {retried.project_id}\n"
                    f"State: {retried.state}\n"
                    f"Host: {retried.assigned_host or '-'}\n"
                    f"Session: {retried.external_session_id or '-'}"
                )
            if state in {
                JobState.WAITING_HOST,
                JobState.WAITING_LEASE,
                JobState.WAITING_QUOTA,
            }:
                retried = await self.jobs.retry_waiting(original.id)
                return (
                    f"↻ 대기 Job 즉시 재시도 요청\n"
                    f"Job: {retried.id}\n"
                    f"Project: {retried.project_id}\n"
                    f"State: {retried.state}\n"
                    f"Host: {retried.assigned_host or '-'}\n"
                    f"Session: {retried.external_session_id or '-'}"
                )
            if state == JobState.WAITING_HUMAN:
                raise ValueError(
                    "WAITING_HUMAN Job은 /retry로 승인 단계를 건너뛸 수 없습니다. "
                    "Human Gate에 응답하세요."
                )
            raise ValueError(
                f"job {original.id} is {state.value}; "
                "/retry supports FAILED, WAITING_HOST, WAITING_LEASE, or WAITING_QUOTA"
            )
        if command.intent == Intent.PAUSE:
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                states={JobState.RUNNING},
                project_id=project_id,
            )
            paused = await self.jobs.pause(job.id)
            return (
                f"⏸ {paused.id} 일시정지됨\n"
                f"Session: {paused.external_session_id or '-'}\n"
                "/resume 으로 같은 Codex session 재개를 시도할 수 있습니다."
            )
        if command.intent == Intent.RESUME:
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                states={JobState.PAUSED},
                project_id=project_id,
            )
            resumed = await self.jobs.resume(job.id)
            return (
                f"▶ {resumed.id} 재개 요청\n"
                f"Host: {resumed.assigned_host or '-'}\n"
                f"Session: {resumed.external_session_id or '-'}"
            )
        if command.intent == Intent.STEER:
            assert command.instruction is not None
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                states=_STEERABLE_STATES,
                project_id=project_id,
            )
            await self.jobs.steer(job.id, command.instruction)
            return (
                f"↪ {job.id} 추가 지시 접수\n"
                "현재 turn을 강제 종료하지 않고, 다음 Codex turn에서 같은 session에 적용합니다."
            )
        if command.intent == Intent.REDIRECT:
            assert command.instruction is not None
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                states={JobState.RUNNING},
                project_id=project_id,
            )
            redirected = await self.jobs.redirect(job.id, command.instruction)
            return (
                f"↪ {redirected.id} 즉시 방향전환 요청\n"
                "현재 Codex turn을 종료하고 같은 session에서 새 지시로 재개했습니다."
            )
        if command.intent == Intent.STOP:
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                project_id=project_id,
            )
            stopped = await self.jobs.cancel(job.id)
            return f"⏹ {stopped.id} 중지됨 ({stopped.state})"
        raise RuntimeError(f"unsupported intent: {command.intent}")


def _job_started_text(job) -> str:
    if job.state == JobState.WAITING_LEASE.value:
        return (
            f"⏳ {job.project_id} 작업 대기열 등록\n"
            f"Job: {job.id}\n"
            f"Host: {job.assigned_host or '-'}\n"
            "앞선 작업이 끝나면 자동으로 시작합니다."
        )
    return (
        f"▶ {job.project_id} 작업 시작\n"
        f"Job: {job.id}\n"
        f"Host: {job.assigned_host}\n"
        "Agent: Codex"
    )


def _is_bare_run(text: str) -> bool:
    parts = text.split()
    if len(parts) != 1:
        return False
    return parts[0].split("@", 1)[0].casefold() == "/run"


def _recovery_suffix(record: RecoveryRecord | None) -> str:
    if record is None:
        return ""
    parts = [f"recovery={record.kind}", f"attempt={record.attempt_count}"]
    if record.last_error:
        reason = " ".join(record.last_error.split())
        parts.append(f"reason={reason[:160]}")
    if record.next_retry_at is not None:
        parts.append(f"retry_at={record.next_retry_at.isoformat()}")
    return " " + " ".join(parts)


def _recovery_detail(record: RecoveryRecord | None) -> str:
    if record is None:
        return ""
    next_retry = record.next_retry_at.isoformat() if record.next_retry_at else "-"
    return (
        f"\nRecovery: {record.kind}"
        f"\nRecovery mode: {record.mode}"
        f"\nRetry attempt: {record.attempt_count}"
        f"\nNext retry: {next_retry}"
        f"\nRecovery error: {record.last_error or '-'}"
    )


def _approval_response_text(
    approval_id: str,
    selected_option: str | None,
    rejected: bool,
) -> str:
    if rejected:
        return (
            f"⛔ Human Gate {approval_id} 거절됨\n"
            "같은 Codex session에 거절 결정을 전달하고 안전한 대안을 요청합니다."
        )
    return (
        f"✅ Human Gate {approval_id} 응답 완료\n"
        f"선택: {selected_option}\n"
        "같은 Codex session으로 작업을 재개합니다."
    )
