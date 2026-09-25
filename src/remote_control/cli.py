from __future__ import annotations

import asyncio
import logging
import platform
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from remote_control.api.app import create_app
from remote_control.approvals.registry import ApprovalRegistry
from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.diagnostics import (
    collect_diagnostics,
    format_diagnostics,
    validate_controller_configuration,
    validate_startup,
)
from remote_control.execution_leases import ExecutionLeaseRegistry
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
from remote_control.sessions.project_sessions import ProjectSessionRegistry
from remote_control.sessions.registry import SessionRegistry
from remote_control.settings import Settings
from remote_control.storage.db import Database
from remote_control.storage.repositories import (
    ApprovalRepository,
    EventRepository,
    ExecutionLeaseRepository,
    HostRepository,
    JobRepository,
    ProjectSessionRepository,
    ProjectWorkRepository,
    RecoveryRepository,
    SessionRepository,
    TelegramMessageBindingRepository,
    TelegramProjectTopicRepository,
)
from remote_control.transport.runner_ws import RunnerGateway

app = typer.Typer(help="Remote Agent Control")
controller_app = typer.Typer(help="Controller commands")
project_app = typer.Typer(help="Project registry commands")
app.add_typer(controller_app, name="controller")
app.add_typer(project_app, name="project")


@app.command("doctor")
def doctor(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", help="Explicit .env file path"),
    ] = None,
) -> None:
    """Check local runtime configuration, executables and project paths."""
    settings = Settings(_env_file=env_file or ".env")
    projects = (
        ProjectRegistry.from_yaml(settings.resolved_config_path)
        if settings.resolved_config_path.exists()
        else None
    )
    typer.echo(format_diagnostics(collect_diagnostics(settings, projects=projects)))


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
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", help="Explicit .env file path"),
    ] = None,
) -> None:
    """Register a project in the Remote Control registry."""
    settings = Settings(_env_file=env_file or ".env")
    project = add_project(
        settings.resolved_config_path,
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
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", help="Explicit .env file path"),
    ] = None,
) -> None:
    """Start the controller."""
    asyncio.run(_run_controller(no_telegram=no_telegram, env_file=env_file))


async def _run_controller(*, no_telegram: bool, env_file: Path | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings(_env_file=env_file or ".env")
    validate_controller_configuration(settings, no_telegram=no_telegram)

    projects = ProjectRegistry.from_yaml(settings.resolved_config_path)
    validate_startup(settings, projects)

    logging.info("runtime home: %s", settings.resolved_home_path)
    logging.info("project config: %s", settings.resolved_config_path)
    logging.info("database: %s", settings.resolved_db_url)

    db = Database(settings.resolved_db_url)
    await db.init()
    events = EventRepository(db)
    execution_leases = ExecutionLeaseRegistry(
        leases=ExecutionLeaseRepository(db),
        events=events,
    )
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
    project_sessions = ProjectSessionRegistry(
        sessions=ProjectSessionRepository(db),
        events=events,
    )
    approvals = ApprovalRegistry(
        approvals=ApprovalRepository(db),
        events=events,
    )
    recovery = RecoveryRepository(db)
    project_work = ProjectWorkRepository(db)
    telegram_topics = TelegramProjectTopicRepository(db)
    telegram_bindings = TelegramMessageBindingRepository(db)

    local_runner = CodexRunner(
        executable=settings.codex_executable,
        sandbox=settings.codex_sandbox,
        approval_policy=settings.codex_approval_policy,
        codex_home=settings.codex_home,
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
        project_sessions=project_sessions,
        approvals=approvals,
        recovery=recovery,
        project_adapters=project_adapters,
        project_work=project_work,
        execution_leases=execution_leases,
        progress_interval_seconds=settings.progress_interval_seconds,
        quota_retry_initial_seconds=settings.quota_retry_initial_seconds,
        quota_retry_max_seconds=settings.quota_retry_max_seconds,
        quota_reset_grace_seconds=settings.quota_reset_grace_seconds,
        restart_grace_seconds=settings.restart_grace_seconds,
    )
    controller = ControllerService(
        projects=projects,
        jobs=manager,
        hosts=hosts,
        diagnostics=lambda: format_diagnostics(
            collect_diagnostics(
                settings,
                projects=projects,
                run_versions=False,
            )
        ),
    )
    scheduler = RecoveryScheduler(
        jobs=manager,
        hosts=hosts,
        interval_seconds=settings.scheduler_interval_seconds,
    )

    await manager.reconcile_startup()

    telegram: TelegramProvider | None = None
    if not no_telegram:
        assert settings.telegram_bot_token is not None
        telegram = TelegramProvider(
            token=settings.telegram_bot_token,
            allowed_user_ids=settings.telegram_allowed_user_ids,
            controller=controller,
            topics=telegram_topics,
            bindings=telegram_bindings,
            selection_ttl_seconds=settings.telegram_selection_ttl_seconds,
        )

    slack: SlackProvider | None = None
    if settings.slack_enabled:
        assert settings.slack_bot_token is not None
        assert settings.slack_app_token is not None
        slack = SlackProvider(
            bot_token=settings.slack_bot_token,
            app_token=settings.slack_app_token,
            allowed_user_ids=settings.slack_allowed_user_ids,
            controller=controller,
        )

    api = create_app(
        controller,
        runner_gateway=gateway,
        runner_token=settings.runner_token,
        api_token=settings.api_token,
        web_ui_enabled=settings.web_ui_enabled,
    )
    config = uvicorn.Config(
        api,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    started_providers = []

    try:
        await scheduler.start()
        if telegram is not None:
            await telegram.start()
            started_providers.append(telegram)
        if slack is not None:
            await slack.start()
            started_providers.append(slack)
        await server.serve()
    finally:
        await scheduler.stop()
        try:
            await manager.shutdown()
        finally:
            for provider in reversed(started_providers):
                await provider.stop()
            await db.close()


if __name__ == "__main__":
    app()
