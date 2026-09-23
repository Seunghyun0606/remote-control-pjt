from __future__ import annotations

import asyncio

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.repositories import EventRepository, JobRepository


@pytest.mark.asyncio
async def test_job_completes(project_registry, database):
    runner = FakeAgentRunner()
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=runner,
        local_host_id="lightsail-main",
    )
    job = await manager.create(
        project_id="demo",
        instruction="do the work",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    for _ in range(100):
        current = await manager.require(job.id)
        if current.state == "COMPLETED":
            await manager.wait_until_idle(job.id)
            break
        await asyncio.sleep(0.01)

    current = await manager.require(job.id)
    assert current.state == "COMPLETED"
    assert current.external_session_id == "fake-session"
    assert runner.started[0]["project_id"] == "demo"


@pytest.mark.asyncio
async def test_remote_host_rejected_in_r0(project_registry, database):
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="other-host",
    )
    with pytest.raises(ValueError, match="not allowed|R0 supports"):
        await manager.create(
            project_id="demo",
            instruction="do the work",
            requested_by_channel="test",
            requested_by_user="u1",
        )


@pytest.mark.asyncio
async def test_cancel_running_job(project_registry, database):
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(delay=1),
        local_host_id="lightsail-main",
    )
    job = await manager.create(
        project_id="demo",
        instruction="do the work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    for _ in range(100):
        current = await manager.require(job.id)
        if current.state == "RUNNING":
            break
        await asyncio.sleep(0.01)

    cancelled = await manager.cancel(job.id)
    await manager.wait_until_idle(job.id)
    assert cancelled.state == "CANCELLED"
