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

    job = (await manager.list(limit=1))[0]
    for _ in range(300):
        current = await manager.require(job.id)
        if current.state == "COMPLETED":
            break
        await asyncio.sleep(0.01)
    assert (await manager.require(job.id)).state == "COMPLETED"
    await manager.wait_until_idle(job.id)
