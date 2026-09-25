from __future__ import annotations

import asyncio

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.registry import SessionRegistry, SessionStatus
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    SessionRepository,
)


async def wait_for_state(
    manager: JobManager,
    job_id: str,
    expected: str,
    *,
    attempts: int = 200,
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


def build_manager(project_registry, database, runner: FakeAgentRunner) -> JobManager:
    events = EventRepository(database)
    sessions = SessionRegistry(
        sessions=SessionRepository(database),
        events=events,
    )
    return JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=sessions,
        progress_interval_seconds=0,
    )


@pytest.mark.asyncio
async def test_pause_and_resume_same_session(project_registry, database):
    runner = FakeAgentRunner(delay=0.2)
    manager = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "RUNNING")

    paused = await manager.pause(job.id)
    assert paused.state == "PAUSED"
    assert paused.external_session_id == "fake-session"

    resumed = await manager.resume(job.id, instruction="continue safely")
    assert resumed.state == "RUNNING"
    await wait_for_state(manager, job.id, "COMPLETED")

    assert runner.resumed
    assert runner.resumed[0]["session_id"] == "fake-session"
    session = await manager.sessions.get_for_job(job.id)
    assert session is not None
    assert session.status == SessionStatus.IDLE.value


@pytest.mark.asyncio
async def test_steering_is_applied_on_next_turn(project_registry, database):
    runner = FakeAgentRunner(delay=0.2)
    manager = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "RUNNING")

    await manager.steer(job.id, "UI는 건드리지 말고 backend만 수정해")
    await wait_for_state(manager, job.id, "COMPLETED")

    assert runner.resumed
    assert "backend" in runner.resumed[0]["instruction"]


@pytest.mark.asyncio
async def test_missing_resume_session_falls_back_to_new_session(project_registry, database):
    runner = FakeAgentRunner(
        delay=0.2,
        resume_returncode=1,
        resume_final_message="No saved session found for thread fake-session",
    )
    manager = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "RUNNING")
    await manager.pause(job.id)

    await manager.resume(job.id, instruction="continue after pause")
    await wait_for_state(manager, job.id, "COMPLETED")

    assert len(runner.resumed) == 1
    assert len(runner.started) == 2
    assert "previous Codex session is unavailable" in runner.started[-1]["instruction"]


@pytest.mark.asyncio
async def test_unknown_resume_failure_does_not_start_new_session(
    project_registry,
    database,
):
    runner = FakeAgentRunner(
        delay=0.05,
        resume_returncode=2,
        resume_final_message="unexpected argument '--sandbox'",
    )
    manager = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "RUNNING")
    await manager.pause(job.id)

    await manager.resume(job.id, instruction="continue after pause")
    await wait_for_state(manager, job.id, "FAILED")

    current = await manager.require(job.id)
    assert "without safe fallback" in (current.error or "")
    assert len(runner.resumed) == 1
    assert len(runner.started) == 1


@pytest.mark.asyncio
async def test_session_identity_mismatch_allows_new_session_fallback(
    project_registry,
    database,
):
    runner = FakeAgentRunner(
        delay=0.05,
        resume_returncode=65,
        resume_session_id="other-thread",
        resume_final_message=(
            "SESSION_IDENTITY_MISMATCH expected=fake-session actual=other-thread"
        ),
    )
    manager = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "RUNNING")
    await manager.pause(job.id)

    await manager.resume(job.id, instruction="continue after mismatch")
    await wait_for_state(manager, job.id, "COMPLETED")

    assert len(runner.resumed) == 1
    assert len(runner.started) == 2
    current = await manager.require(job.id)
    assert current.external_session_id == "fake-session"


@pytest.mark.asyncio
async def test_resume_rebinds_when_codex_returns_new_thread(project_registry, database):
    runner = FakeAgentRunner(delay=0.2, resume_session_id="new-thread")
    manager = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="initial work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "RUNNING")
    await manager.pause(job.id)

    await manager.resume(job.id)
    await wait_for_state(manager, job.id, "COMPLETED")

    current = await manager.require(job.id)
    assert current.external_session_id == "new-thread"
    session = await manager.sessions.get_for_job(job.id)
    assert session is not None
    assert session.external_session_id == "new-thread"
