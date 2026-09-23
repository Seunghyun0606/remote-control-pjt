from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import EventRepository, JobRepository, RecoveryRepository


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



@pytest.mark.asyncio
async def test_status_and_job_show_quota_retry(project_registry, database):
    recovery = RecoveryRepository(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        recovery=recovery,
    )
    job = JobRecord(
        id="JOB-QUOTA-STATUS",
        project_id="demo",
        requested_by_channel="telegram",
        requested_by_user="100",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="continue",
        state="WAITING_QUOTA",
        external_session_id="thread-quota",
    )
    await manager.jobs.add(job)
    retry_at = datetime(2026, 9, 25, 5, 30, tzinfo=timezone.utc)
    await recovery.upsert(
        job.id,
        kind="QUOTA",
        mode="RESUME",
        attempt_count=2,
        next_retry_at=retry_at,
        execution_id=None,
        resume_instruction="resume after quota",
        last_error="usage_limit_exceeded",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    status = await controller.handle_text(
        "/status",
        channel="telegram",
        user_id="100",
    )
    assert "WAITING_QUOTA" in status
    assert "recovery=QUOTA" in status
    assert "attempt=2" in status
    assert "retry_at=" in status

    detail = await controller.handle_text(
        "/job JOB-QUOTA-STATUS",
        channel="telegram",
        user_id="100",
    )
    assert "Recovery: QUOTA" in detail
    assert "Recovery mode: RESUME" in detail
    assert "Retry attempt: 2" in detail
    assert "Next retry:" in detail
