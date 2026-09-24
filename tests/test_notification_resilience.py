from __future__ import annotations

import asyncio
import json
import logging

import pytest
from sqlalchemy import select

from remote_control.controller.job_manager import JobManager
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.models import EventRecord
from remote_control.storage.repositories import EventRepository, JobRepository


def build_manager(*, project_registry, database, runner=None) -> JobManager:
    return JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=runner or FakeAgentRunner(),
        local_host_id="lightsail-main",
    )


async def notification_failures(database, job_id: str) -> list[EventRecord]:
    async with database.sessions() as session:
        result = await session.execute(
            select(EventRecord)
            .where(EventRecord.job_id == job_id)
            .where(EventRecord.event_type == "NOTIFICATION_FAILED")
            .order_by(EventRecord.id)
        )
        return list(result.scalars())


@pytest.mark.asyncio
async def test_notification_timeout_does_not_fail_completed_job(
    project_registry,
    database,
    monkeypatch,
):
    monkeypatch.setattr(
        "remote_control.controller.job_manager._NOTIFICATION_RETRY_DELAYS",
        (0, 0),
    )
    manager = build_manager(project_registry=project_registry, database=database)
    attempts = 0

    async def failing_notifier(user_id: str, text: str) -> None:
        nonlocal attempts
        del user_id, text
        attempts += 1
        raise TimeoutError("telegram connect timed out")

    manager.set_notifier(failing_notifier)

    job = await manager.create(
        project_id="demo",
        instruction="complete normally",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(job.id)

    current = await manager.require(job.id)
    assert current.state == "COMPLETED"
    assert current.error is None
    assert attempts == 3

    failures = await notification_failures(database, job.id)
    assert len(failures) == 1
    payload = json.loads(failures[0].payload_json)
    assert payload["channel"] == "telegram"
    assert payload["notification_type"] == "message"
    assert payload["attempts"] == 3
    assert payload["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_notification_retries_then_recovers_without_failure_event(
    project_registry,
    database,
    monkeypatch,
):
    monkeypatch.setattr(
        "remote_control.controller.job_manager._NOTIFICATION_RETRY_DELAYS",
        (0, 0),
    )
    manager = build_manager(project_registry=project_registry, database=database)
    attempts = 0

    async def flaky_notifier(user_id: str, text: str) -> None:
        nonlocal attempts
        del user_id, text
        attempts += 1
        if attempts < 3:
            raise TimeoutError("temporary telegram timeout")

    manager.set_notifier(flaky_notifier)

    job = await manager.create(
        project_id="demo",
        instruction="complete after notification retries",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(job.id)

    current = await manager.require(job.id)
    assert current.state == "COMPLETED"
    assert attempts == 3
    assert await notification_failures(database, job.id) == []


@pytest.mark.asyncio
async def test_notification_failure_does_not_replace_agent_failure(
    project_registry,
    database,
    monkeypatch,
):
    monkeypatch.setattr(
        "remote_control.controller.job_manager._NOTIFICATION_RETRY_DELAYS",
        (0, 0),
    )
    manager = build_manager(
        project_registry=project_registry,
        database=database,
        runner=FakeAgentRunner(returncode=1),
    )

    async def failing_notifier(user_id: str, text: str) -> None:
        del user_id, text
        raise TimeoutError("telegram connect timed out")

    manager.set_notifier(failing_notifier)

    job = await manager.create(
        project_id="demo",
        instruction="agent fails",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(job.id)

    current = await manager.require(job.id)
    assert current.state == "FAILED"
    assert current.error == "fake failed"
    assert len(await notification_failures(database, job.id)) == 1


@pytest.mark.asyncio
async def test_background_task_exception_is_consumed_and_logged(
    project_registry,
    database,
    caplog,
):
    manager = build_manager(project_registry=project_registry, database=database)

    async def explode() -> None:
        raise RuntimeError("unexpected background failure")

    with caplog.at_level(logging.ERROR):
        manager._start_task("JOB-BACKGROUND-FAILURE", explode())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "JOB-BACKGROUND-FAILURE" not in manager._tasks
    assert "Unhandled Job task exception job_id=JOB-BACKGROUND-FAILURE" in caplog.text
    assert "unexpected background failure" in caplog.text
