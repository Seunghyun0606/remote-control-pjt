from __future__ import annotations

import asyncio
import logging

import typer

from remote_control.runner_daemon import RunnerDaemon
from remote_control.settings import RunnerSettings

app = typer.Typer(help="Remote Agent Runner")


@app.command("start")
def start() -> None:
    """Connect this host to a Remote Agent Controller."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = RunnerSettings()
    if not settings.token:
        raise RuntimeError("REMOTE_RUNNER_TOKEN is required")
    asyncio.run(RunnerDaemon(settings).run_forever())


if __name__ == "__main__":
    app()
