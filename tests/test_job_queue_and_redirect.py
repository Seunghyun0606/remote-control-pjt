from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.states import JobState
from remote_control.execution_leases import ExecutionLeaseRegistry
from remote_control.recovery.models import RecoveryKind, RecoveryMode
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.project_sessions import ProjectSessionRegistry
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import (
    EventRepository,
    ExecutionLeaseRepository,
    JobRepository,
    ProjectSessionRepository,
    RecoveryRepository,
)


async def _wait_for_state(
    manager: JobManager,
    job_id: str,
    expected: str,
    *,
    attempts: int = 300,
) -> None:
    for _ in range(attempts):
        current = await manager.require(job_id)
        if current.state == expected:
            if expected in {"COMPLETED", "FAILED", "CANCELLED"}:
                await manager.wait_until_idle(job_id)
            return
        await asyncio.sleep(0.01)
    current = await manager.require(job_id)
    raise AssertionError(f"expected {expected}, got {current.state}")


def _manager(project_registry, database, runner: FakeAgentRunner) -> JobManager:
    events = EventRepository(database)
    return JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        recovery=RecoveryRepository(database),
        project_sessions=ProjectSessionRegistry(
            sessions=ProjectSessionRepository(database),
            events=events,
        ),
        execution_leases=ExecutionLeaseRegistry(
            leases=ExecutionLeaseRepository(database),
            events=events,
        ),
        progress_interval_seconds=0,
    )


@pytest.mark.asyncio
async def test_busy_working_tree_jobs_wait_and_run_in_fifo_order(
    project_registry,
    database,
):
    runner = FakeAgentRunner(delay=0.15)
    manager = _manager(project_registry, database, runner)

    first = await manager.create(
        project_id="demo",
        instruction="first",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await _wait_for_state(manager, first.id, "RUNNING")

    second = await manager.create(
        project_id="demo",
        instruction="second",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    third = await manager.create(
        project_id="demo",
        instruction="third",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    assert second.state == "WAITING_LEASE"
    assert third.state == "WAITING_LEASE"
    second_recovery = await manager.recovery.get(second.id)
    third_recovery = await manager.recovery.get(third.id)
    assert second_recovery is not None
    assert third_recovery is not None
    assert second_recovery.kind == RecoveryKind.LEASE.value
    assert third_recovery.kind == RecoveryKind.LEASE.value
    assert await manager.execution_leases.get_for_job(second.id) is None
    assert await manager.execution_leases.get_for_job(third.id) is None

    await _wait_for_state(manager, first.id, "COMPLETED")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert await manager.recover_due(now=now) == 1

    await _wait_for_state(manager, second.id, "RUNNING")
    assert (await manager.require(third.id)).state == "WAITING_LEASE"
    await _wait_for_state(manager, second.id, "COMPLETED")

    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert await manager.recover_due(now=now) == 1
    await _wait_for_state(manager, third.id, "COMPLETED")

    assert len(runner.started) == 1
    resumed_instructions = [item["instruction"] for item in runner.resumed[-2:]]
    assert "second" in resumed_instructions[0]
    assert "third" in resumed_instructions[1]


@pytest.mark.asyncio
async def test_redirect_stops_current_turn_and_resumes_same_session(
    project_registry,
    database,
):
    runner = FakeAgentRunner(delay=0.25)
    manager = _manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await _wait_for_state(manager, job.id, "RUNNING")
    before = await manager.require(job.id)
    assert before.external_session_id == "fake-session"

    redirected = await manager.redirect(
        job.id,
        "stop the current direction and change the database layer first",
    )

    assert redirected.state == "RUNNING"
    assert redirected.external_session_id == "fake-session"
    await _wait_for_state(manager, job.id, "COMPLETED")

    assert runner.resumed
    assert runner.resumed[-1]["session_id"] == "fake-session"
    assert "database layer first" in runner.resumed[-1]["instruction"]


@pytest.mark.asyncio
async def test_waiting_lease_is_rearmed_after_controller_restart(
    project_registry,
    database,
):
    runner = FakeAgentRunner()
    manager = _manager(project_registry, database, runner)
    await manager.jobs.add(
        JobRecord(
            id="JOB-LEASE-RESTART",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="u1",
            requested_host="lightsail-main",
            assigned_host="lightsail-main",
            instruction="queued work",
            state=JobState.WAITING_LEASE.value,
        )
    )

    await manager.reconcile_startup(
        now=datetime.now(timezone.utc) + timedelta(seconds=1)
    )

    current = await manager.require("JOB-LEASE-RESTART")
    assert current.state == "WAITING_LEASE"
    recovery = await manager.recovery.get("JOB-LEASE-RESTART")
    assert recovery is not None
    assert recovery.kind == RecoveryKind.LEASE.value
    assert recovery.next_retry_at is not None


@pytest.mark.asyncio
async def test_host_recovery_reacquires_project_session_before_execution(
    project_registry,
    database,
):
    runner = FakeAgentRunner(delay=0.15)
    manager = _manager(project_registry, database, runner)
    await manager.jobs.add(
        JobRecord(
            id="JOB-RECOVER-SESSION",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="u1",
            requested_host="lightsail-main",
            assigned_host="lightsail-main",
            instruction="recover safely",
            state=JobState.WAITING_HOST.value,
            external_session_id="fake-session",
        )
    )
    await manager.recovery.upsert(
        "JOB-RECOVER-SESSION",
        kind=RecoveryKind.RESTART.value,
        mode=RecoveryMode.START.value,
        attempt_count=0,
        next_retry_at=datetime.now(timezone.utc),
        execution_id=None,
        resume_instruction=None,
        last_error="restart after lease assignment",
    )

    recovered = await manager.recover_due(
        now=datetime.now(timezone.utc) + timedelta(seconds=1)
    )
    assert recovered == 1
    await _wait_for_state(manager, "JOB-RECOVER-SESSION", "RUNNING")

    project_session = await manager.project_sessions.active_for("demo", "u1")
    assert project_session is not None
    assert project_session.locked_by_job_id == "JOB-RECOVER-SESSION"

    await _wait_for_state(manager, "JOB-RECOVER-SESSION", "COMPLETED")
