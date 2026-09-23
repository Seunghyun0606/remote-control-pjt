from __future__ import annotations

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


class ControllerService:
    def __init__(
        self,
        *,
        projects: ProjectRegistry,
        jobs: JobManager,
        hosts: HostRegistry | None = None,
    ) -> None:
        self.projects = projects
        self.jobs = jobs
        self.hosts = hosts
        self.router = CommandRouter(projects)

    async def handle_text(self, text: str, *, channel: str, user_id: str) -> str:
        stripped = text.strip()
        if stripped and not stripped.startswith("/"):
            pending = await self.jobs.pending_approvals_for_user(user_id)
            if len(pending) == 1:
                resolved = await self.jobs.match_pending_approval(user_id, stripped)
                if resolved is not None:
                    return _approval_response_text(resolved.id, resolved.selected_option, False)
                options = ", ".join(
                    option.key for option in self.jobs.approvals.options(pending[0])
                )
                return (
                    f"⚠ Human Gate 응답 대기 중입니다. "
                    f"선택지({options}) 또는 Telegram 버튼을 사용하세요."
                )

        command = self.router.parse(text)
        return await self.execute(command, channel=channel, user_id=user_id)

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

    async def execute(self, command: Command, *, channel: str, user_id: str) -> str:
        if command.intent == Intent.HELP:
            return (
                "Remote Agent Control\n"
                "/projects\n/status\n/hosts\n/run <project> [--host <host-id>]\n"
                "/jobs\n/job <job-id>\n/pause [job-id]\n/resume [job-id]\n"
                "/steer [--job <job-id>] <instruction>\n/stop [job-id]"
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
        if command.intent == Intent.STATUS:
            active = await self.jobs.active_for_user(user_id)
            if not active:
                return "현재 active Job이 없습니다."
            lines = []
            for job in active:
                recovery = await self.jobs.recovery_for(job.id)
                lines.append(
                    f"{job.id} {job.project_id} {job.state} "
                    f"host={job.assigned_host or '-'}{_recovery_suffix(recovery)}"
                )
            return "\n".join(lines)
        if command.intent == Intent.RUN_PROJECT:
            assert command.project_id is not None
            job = await self.jobs.create(
                project_id=command.project_id,
                instruction=DEFAULT_INSTRUCTION,
                requested_by_channel=channel,
                requested_by_user=user_id,
                requested_host=command.host,
            )
            return (
                f"▶ {job.project_id} 작업 시작\n"
                f"Job: {job.id}\n"
                f"Host: {job.assigned_host}\n"
                "Agent: Codex"
            )
        if command.intent == Intent.JOBS:
            jobs = await self.jobs.list(limit=20)
            if not jobs:
                return "Job이 없습니다."
            lines = []
            for job in jobs:
                recovery = await self.jobs.recovery_for(job.id)
                lines.append(
                    f"{job.id} {job.project_id} {job.state} "
                    f"host={job.assigned_host or '-'}{_recovery_suffix(recovery)}"
                )
            return "\n".join(lines)
        if command.intent == Intent.JOB:
            assert command.job_id is not None
            job = await self.jobs.require(command.job_id)
            work = await self.jobs.project_work_for(job.id)
            recovery = await self.jobs.recovery_for(job.id)
            task_line = f"\nTask: {work.task_id}" if work and work.task_id else ""
            adapter_line = f"\nAdapter: {work.adapter}" if work else ""
            return (
                f"{job.id}\nProject: {job.project_id}\nState: {job.state}\n"
                f"Host: {job.assigned_host or '-'}\nSession: {job.external_session_id or '-'}"
                f"{adapter_line}{task_line}{_recovery_detail(recovery)}"
            )
        if command.intent == Intent.PAUSE:
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
                states={JobState.RUNNING},
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
                states={JobState.ASSIGNED, JobState.STARTING, JobState.RUNNING},
            )
            await self.jobs.steer(job.id, command.instruction)
            return (
                f"↪ {job.id} 추가 지시 접수\n"
                "현재 turn을 강제 종료하지 않고, 다음 Codex turn에서 같은 session에 적용합니다."
            )
        if command.intent == Intent.STOP:
            job = await self.jobs.select_for_user(
                user_id,
                job_id=command.job_id,
            )
            stopped = await self.jobs.cancel(job.id)
            return f"⏹ {stopped.id} 중지됨 ({stopped.state})"
        raise RuntimeError(f"unsupported intent: {command.intent}")



def _recovery_suffix(record: RecoveryRecord | None) -> str:
    if record is None:
        return ""
    parts = [f" recovery={record.kind}", f"attempt={record.attempt_count}"]
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
