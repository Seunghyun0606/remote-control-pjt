from __future__ import annotations

import asyncio
import logging

import typer
import uvicorn

from remote_control.api.app import create_app
from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.messaging.telegram import TelegramProvider
from remote_control.projects.registry import ProjectRegistry
from remote_control.runners.codex import CodexRunner
from remote_control.settings import Settings
from remote_control.storage.db import Database
from remote_control.storage.repositories import EventRepository, JobRepository

app = typer.Typer(help="Remote Agent Control")
controller_app = typer.Typer(help="Controller commands")
app.add_typer(controller_app, name="controller")


@controller_app.command("start")
def controller_start(
    no_telegram: bool = typer.Option(False, "--no-telegram", help="Do not start Telegram polling"),
) -> None:
    """Start the R0 controller."""
    asyncio.run(_run_controller(no_telegram=no_telegram))


async def _run_controller(*, no_telegram: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    db = Database(settings.db_url)
    await db.init()

    projects = ProjectRegistry.from_yaml(settings.config_path)
    runner = CodexRunner(
        executable=settings.codex_executable,
        sandbox=settings.codex_sandbox,
        approval_policy=settings.codex_approval_policy,
    )
    manager = JobManager(
        projects=projects,
        jobs=JobRepository(db),
        events=EventRepository(db),
        runner=runner,
        local_host_id=settings.host_id,
    )
    controller = ControllerService(projects=projects, jobs=manager)

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

    api = create_app(controller)
    config = uvicorn.Config(
        api,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        if telegram is not None:
            await telegram.stop()
        await db.close()


if __name__ == "__main__":
    app()
