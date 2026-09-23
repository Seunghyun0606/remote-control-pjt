from __future__ import annotations

import asyncio

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.registry import SessionRegistry
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    SessionRepository,
)


@pytest.mark.asyncio
async def test_plain_text_steers_single_running_job(project_registry, database):
    events = EventRepository(database)
    runner = FakeAgentRunner(delay=0.05)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=SessionRegistry(
            sessions=SessionRepository(database),
            events=events,
        ),
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    response = await controller.handle_text(
        "/run demo",
        channel="telegram",
        user_id="100",
    )
    assert "작업 시작" in response

    steer = await controller.handle_text(
        "UI는 건드리지 말고 backend만 수정해",
        channel="telegram",
        user_id="100",
    )
    assert "추가 지시 접수" in steer

    job_id = (await manager.list(limit=1))[0].id
    for _ in range(500):
        current = await manager.require(job_id)
        if current.state == "COMPLETED":
            break
        await asyncio.sleep(0.01)

    current = await manager.require(job_id)
    assert current.state == "COMPLETED"
    await manager.wait_until_idle(job_id)

    assert runner.resumed
    assert "backend" in runner.resumed[0]["instruction"]


@pytest.mark.asyncio
async def test_plain_text_without_active_job_is_rejected(project_registry, database):
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    with pytest.raises(ValueError, match="matching active job not found"):
        await controller.handle_text(
            "rm -rf /",
            channel="telegram",
            user_id="100",
        )
