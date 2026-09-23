from __future__ import annotations

import asyncio

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.repositories import EventRepository, JobRepository


@pytest.mark.asyncio
async def test_controller_run_and_status(project_registry, database):
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(delay=0.05),
        local_host_id="lightsail-main",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    response = await controller.handle_text(
        "/run demo",
        channel="telegram",
        user_id="100",
    )
    assert "작업 시작" in response

    status = await controller.handle_text(
        "/status",
        channel="telegram",
        user_id="100",
    )
    assert "demo" in status
    await asyncio.sleep(0.1)
