from __future__ import annotations

from remote_control.controller.command_router import Command, CommandRouter, Intent
from remote_control.controller.job_manager import JobManager
from remote_control.hosts.registry import HostRegistry
from remote_control.projects.registry import ProjectRegistry


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
        command = self.router.parse(text)
        return await self.execute(command, channel=channel, user_id=user_id)

    async def execute(self, command: Command, *, channel: str, user_id: str) -> str:
        if command.intent == Intent.HELP:
            return (
                "Remote Agent Control\n"
                "/projects\n/status\n/hosts\n/run <project> [--host <host-id>]\n"
                "/jobs\n/job <job-id>\n/stop"
            )
        if command.intent == Intent.PROJECTS:
            projects = self.projects.list()
            if not projects:
                return "등록된 프로젝트가 없습니다."
            return "\n".join(f"{project.id} — {project.name}" for project in projects)
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
                return "현재 실행 중인 Job이 없습니다."
            return "\n".join(
                f"{job.id} {job.project_id} {job.state} host={job.assigned_host or '-'}"
                for job in active
            )
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
            return "\n".join(
                f"{job.id} {job.project_id} {job.state} host={job.assigned_host or '-'}"
                for job in jobs
            )
        if command.intent == Intent.JOB:
            assert command.job_id is not None
            job = await self.jobs.require(command.job_id)
            return (
                f"{job.id}\nProject: {job.project_id}\nState: {job.state}\n"
                f"Host: {job.assigned_host or '-'}\nSession: {job.external_session_id or '-'}"
            )
        if command.intent == Intent.STOP:
            active = await self.jobs.active_for_user(user_id)
            if not active:
                return "중지할 active Job이 없습니다."
            job = await self.jobs.cancel(active[0].id)
            return f"⏹ {job.id} 중지됨 ({job.state})"
        raise RuntimeError(f"unsupported intent: {command.intent}")
