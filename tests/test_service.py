from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.project_sessions import ProjectSessionRegistry
from remote_control.sessions.registry import SessionRegistry
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    ProjectSessionRepository,
    RecoveryRepository,
    SessionRepository,
)


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


@pytest.mark.asyncio
async def test_retry_failed_job_creates_new_job_and_resumes_session(
    project_registry,
    database,
):
    runner = FakeAgentRunner(delay=0.05)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=runner,
        local_host_id="lightsail-main",
    )
    failed = JobRecord(
        id="JOB-FAILED-1",
        project_id="demo",
        requested_by_channel="telegram",
        requested_by_user="100",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="continue original work",
        state="FAILED",
        external_session_id="thread-old",
        error="previous failure",
    )
    await manager.jobs.add(failed)
    controller = ControllerService(projects=project_registry, jobs=manager)

    response = await controller.handle_text(
        "/retry JOB-FAILED-1",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert "Retry of: JOB-FAILED-1" in response

    jobs = await manager.list(limit=10)
    retried = next(job for job in jobs if job.id != failed.id)
    assert retried.project_id == "demo"
    assert retried.external_session_id == "thread-old"

    await manager.wait_until_idle(retried.id)
    assert (await manager.require(failed.id)).state == "FAILED"
    assert (await manager.require(retried.id)).state == "COMPLETED"
    assert runner.resumed
    assert runner.resumed[0]["session_id"] == "thread-old"


@pytest.mark.asyncio
async def test_sessions_are_scoped_to_user_and_project(project_registry, database):
    events = EventRepository(database)
    sessions = SessionRegistry(
        sessions=SessionRepository(database),
        events=events,
    )
    project_sessions = ProjectSessionRegistry(
        sessions=ProjectSessionRepository(database),
        events=events,
    )
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(delay=0.01),
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )

    own_job = await manager.create(
        project_id="demo",
        instruction="work",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    other_job = await manager.create(
        project_id="demo",
        instruction="other",
        requested_by_channel="telegram",
        requested_by_user="200",
    )
    await manager.wait_until_idle(own_job.id)
    await manager.wait_until_idle(other_job.id)

    own_session = await project_sessions.active_for("demo", "100")
    assert own_session is not None

    controller = ControllerService(projects=project_registry, jobs=manager)
    listing = await controller.handle_text(
        "/sessions",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert own_session.id in listing
    other_session = await project_sessions.active_for("demo", "200")
    assert other_session is not None
    assert other_session.id not in listing

    detail = await controller.handle_text(
        f"/session {own_session.id}",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert "Codex session: fake-session" in detail


@pytest.mark.asyncio
async def test_status_includes_waiting_host_reason(project_registry, database):
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
        id="JOB-HOST-STATUS",
        project_id="demo",
        requested_by_channel="telegram",
        requested_by_user="100",
        requested_host="auto",
        assigned_host=None,
        instruction="work",
        state="WAITING_HOST",
    )
    await manager.jobs.add(job)
    await recovery.upsert(
        job.id,
        kind="HOST",
        mode="START",
        attempt_count=0,
        next_retry_at=datetime.now(timezone.utc),
        execution_id=None,
        resume_instruction=None,
        last_error="no online host is available for project 'demo'",
    )

    controller = ControllerService(projects=project_registry, jobs=manager)
    status = await controller.handle_text(
        "/status",
        channel="telegram",
        user_id="100",
    )
    assert "WAITING_HOST" in status
    assert "reason=no online host is available" in status
