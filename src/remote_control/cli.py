from __future__ import annotations

import asyncio
import logging
import platform

import typer
import uvicorn

from remote_control.api.app import create_app
from remote_control.approvals.registry import ApprovalRegistry
from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.hosts.registry import HostRegistry
from remote_control.messaging.slack import SlackProvider
from remote_control.messaging.telegram import TelegramProvider
from remote_control.projects.adapters import ProjectAdapterRegistry
from remote_control.projects.config import add_project
from remote_control.projects.operations import (
    HybridProjectOperationExecutor,
    LocalProjectOperationExecutor,
)
from remote_control.projects.registry import ProjectRegistry
from remote_control.recovery.scheduler import RecoveryScheduler
from remote_control.runners.codex import CodexRunner
from remote_control.runners.hybrid import HybridAgentRunner
from remote_control.sessions.registry import SessionRegistry
from remote_control.settings import Settings
from remote_control.storage.db import Database
from remote_control.storage.repositories import (
    ApprovalRepository,
    EventRepository,
    HostRepository,
    JobRepository,
    ProjectWorkRepository,
    RecoveryRepository,
    SessionRepository,
)
from remote_control.transport.runner_ws import RunnerGateway

app = typer.Typer(help="Remote Agent Control")
controller_app = typer.Typer(help="Controller commands")
project_app = typer.Typer(help="Project registry commands")
app.add_typer(controller_app, name="controller")
app.add_typer(project_app, name="project")




@project_app.command("add")
def project_add(
    project_id: str = typer.Option(..., "--id", help="Project id"),
    path: str = typer.Option(..., "--path", help="Working directory on the selected host"),
    adapter: str = typer.Option("generic_git", "--adapter", help="generic_git or project_os"),
    host: str | None = typer.Option(None, "--host", help="Execution host id"),
    name: str | None = typer.Option(None, "--name", help="Display name"),
    role: str = typer.Option("developer", "--role", help="Project OS role"),
    actor: str = typer.Option(
        "remote-control-codex",
        "--actor",
        help="Project OS handoff actor",
    ),
) -> None:
    """Register a project in the Remote Control registry."""
    settings = Settings()
    project = add_project(
        settings.config_path,
        project_id=project_id,
        name=name,
        adapter=adapter,
        host_id=host or settings.host_id,
        working_directory=path,
        role=role,
        actor=actor,
    )
    typer.echo(
        f"registered {project.id} adapter={project.adapter} "
        f"host={project.default_host} path={project.path_for(project.default_host or '')}"
    )


@controller_app.command("start")
def controller_start(
    no_telegram: bool = typer.Option(False, "--no-telegram", help="Do not start Telegram polling"),
) -> None:
    """Start the R6 controller."""
    asyncio.run(_run_controller(no_telegram=no_telegram))


async def _run_controller(*, no_telegram: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    if not settings.runner_token:
        raise RuntimeError("CONTROLLER_RUNNER_TOKEN is required")

    db = Database(settings.db_url)
    await db.init()

    projects = ProjectRegistry.from_yaml(settings.config_path)
    events = EventRepository(db)
    hosts = HostRegistry(
        hosts=HostRepository(db),
        events=events,
        local_host_id=settings.host_id,
        heartbeat_timeout_seconds=settings.heartbeat_timeout_seconds,
    )
    await hosts.register_local(
        name="Controller Local Runner",
        os_name=platform.system().lower(),
        capabilities={"codex", "git", "projectctl", "long_running", "shell"},
    )
    sessions = SessionRegistry(
        sessions=SessionRepository(db),
        events=events,
    )
    approvals = ApprovalRegistry(
        approvals=ApprovalRepository(db),
        events=events,
    )
    recovery = RecoveryRepository(db)
    project_work = ProjectWorkRepository(db)

    local_runner = CodexRunner(
        executable=settings.codex_executable,
        sandbox=settings.codex_sandbox,
        approval_policy=settings.codex_approval_policy,
    )
    gateway = RunnerGateway()
    runner = HybridAgentRunner(
        local_host_id=settings.host_id,
        local_runner=local_runner,
        gateway=gateway,
    )
    project_operations = HybridProjectOperationExecutor(
        local_host_id=settings.host_id,
        local=LocalProjectOperationExecutor(
            projectctl_executable=settings.projectctl_executable,
            git_executable=settings.git_executable,
            timeout_seconds=settings.project_operation_timeout_seconds,
        ),
        gateway=gateway,
        timeout_seconds=settings.project_operation_timeout_seconds,
    )
    project_adapters = ProjectAdapterRegistry(
        operations=project_operations,
        work=project_work,
        events=events,
    )
    manager = JobManager(
        projects=projects,
        jobs=JobRepository(db),
        events=events,
        runner=runner,
        local_host_id=settings.host_id,
        hosts=hosts,
        sessions=sessions,
        approvals=approvals,
        recovery=recovery,
        project_adapters=project_adapters,
        project_work=project_work,
        progress_interval_seconds=settings.progress_interval_seconds,
        quota_retry_initial_seconds=settings.quota_retry_initial_seconds,
        quota_retry_max_seconds=settings.quota_retry_max_seconds,
        restart_grace_seconds=settings.restart_grace_seconds,
    )
    controller = ControllerService(projects=projects, jobs=manager, hosts=hosts)
    scheduler = RecoveryScheduler(
        jobs=manager,
        hosts=hosts,
        interval_seconds=settings.scheduler_interval_seconds,
    )

    await manager.reconcile_startup()

    telegram: TelegramProvider | None = None
    if not no_telegram:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required unless --no-telegram is used")
        telegram = TelegramProvider(
            token=settings.telegram_bot_token,
            allowed_user_ids=settings.telegram_allowed_user_ids,
            controller=controller,
        )
        await telegram.start()

    slack: SlackProvider | None = None
    if settings.slack_enabled:
        if not settings.slack_bot_token or not settings.slack_app_token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required when Slack is enabled"
            )
        if not settings.slack_allowed_user_ids:
            raise RuntimeError(
                "SLACK_ALLOWED_USER_IDS is required when Slack is enabled"
            )
        slack = SlackProvider(
            bot_token=settings.slack_bot_token,
            app_token=settings.slack_app_token,
            allowed_user_ids=settings.slack_allowed_user_ids,
            controller=controller,
        )
        await slack.start()

    api = create_app(
        controller,
        runner_gateway=gateway,
        runner_token=settings.runner_token,
        web_ui_enabled=settings.web_ui_enabled,
    )
    config = uvicorn.Config(
        api,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await scheduler.start()

    try:
        await server.serve()
    finally:
        await scheduler.stop()
        if slack is not None:
            await slack.stop()
        if telegram is not None:
            await telegram.stop()
        await db.close()


if __name__ == "__main__":
    app()
