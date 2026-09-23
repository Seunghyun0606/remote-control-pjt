import asyncio

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.project_sessions import (
    ProjectSessionBusyError,
    ProjectSessionRegistry,
)
from remote_control.sessions.registry import SessionRegistry
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    ProjectSessionRepository,
    SessionRepository,
)


def _registries(database):
    events = EventRepository(database)
    return (
        events,
        SessionRegistry(
            sessions=SessionRepository(database),
            events=events,
        ),
        ProjectSessionRegistry(
            sessions=ProjectSessionRepository(database),
            events=events,
        ),
    )


@pytest.mark.asyncio
async def test_completed_jobs_reuse_one_project_codex_session(project_registry, database):
    events, sessions, project_sessions = _registries(database)
    runner = FakeAgentRunner(delay=0.01)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )

    first = await manager.create(
        project_id="demo",
        instruction="first",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(first.id)
    first = await manager.require(first.id)
    assert first.state == "COMPLETED"
    assert first.external_session_id == "fake-session"

    persistent = await project_sessions.active_for("demo", "100")
    assert persistent is not None
    assert persistent.external_session_id == "fake-session"
    assert persistent.locked_by_job_id is None
    first_project_session_id = persistent.id

    second = await manager.create(
        project_id="demo",
        instruction="continue with next step",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(second.id)
    second = await manager.require(second.id)

    assert second.state == "COMPLETED"
    assert len(runner.started) == 1
    assert len(runner.resumed) == 1
    assert runner.resumed[0]["session_id"] == "fake-session"
    assert "continue with next step" in runner.resumed[0]["instruction"]

    persistent = await project_sessions.active_for("demo", "100")
    assert persistent is not None
    assert persistent.id == first_project_session_id
    assert persistent.last_job_id == second.id
    assert persistent.locked_by_job_id is None


@pytest.mark.asyncio
async def test_project_session_rollover_closes_old_context(project_registry, database):
    events, sessions, project_sessions = _registries(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(delay=0.01),
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    first = await manager.create(
        project_id="demo",
        instruction="seed",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(first.id)
    previous = await project_sessions.active_for("demo", "100")
    assert previous is not None
    assert previous.external_session_id == "fake-session"

    response = await controller.handle_text(
        "/session new",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert "새 Project Session" in response

    current = await project_sessions.active_for("demo", "100")
    assert current is not None
    assert current.id != previous.id
    assert current.external_session_id is None
    closed = await project_sessions.get(previous.id)
    assert closed is not None
    assert closed.status == "CLOSED"


@pytest.mark.asyncio
async def test_project_session_blocks_parallel_jobs(project_registry, database):
    events, sessions, project_sessions = _registries(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(delay=0.2),
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )

    first = await manager.create(
        project_id="demo",
        instruction="long work",
        requested_by_channel="api",
        requested_by_user="100",
    )
    with pytest.raises(ProjectSessionBusyError):
        await manager.create(
            project_id="demo",
            instruction="parallel work",
            requested_by_channel="api",
            requested_by_user="100",
        )

    await manager.wait_until_idle(first.id)


@pytest.mark.asyncio
async def test_project_sessions_are_separate_by_user(project_registry, database):
    events, sessions, project_sessions = _registries(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(delay=0.01),
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )

    one, two = await asyncio.gather(
        manager.create(
            project_id="demo",
            instruction="user one",
            requested_by_channel="telegram",
            requested_by_user="100",
        ),
        manager.create(
            project_id="demo",
            instruction="user two",
            requested_by_channel="telegram",
            requested_by_user="200",
        ),
    )
    await asyncio.gather(
        manager.wait_until_idle(one.id),
        manager.wait_until_idle(two.id),
    )

    session_one = await project_sessions.active_for("demo", "100")
    session_two = await project_sessions.active_for("demo", "200")
    assert session_one is not None
    assert session_two is not None
    assert session_one.id != session_two.id
