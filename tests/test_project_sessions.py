import asyncio

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.runners.base import AgentRunResult, AgentRunner, RunHandle
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.project_sessions import (
    ProjectSessionBusyError,
    ProjectSessionRegistry,
)
from remote_control.sessions.registry import SessionRegistry
from remote_control.storage.models import JobRecord
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



@pytest.mark.asyncio
async def test_legacy_completed_job_seeds_first_project_session(project_registry, database):
    events, sessions, project_sessions = _registries(database)
    jobs = JobRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-LEGACY",
            project_id="demo",
            requested_by_channel="telegram",
            requested_by_user="100",
            requested_host="lightsail-main",
            assigned_host="lightsail-main",
            instruction="old work",
            state="COMPLETED",
            external_session_id="legacy-thread",
        )
    )
    runner = FakeAgentRunner(delay=0.01, resume_session_id="legacy-thread")
    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )

    job = await manager.create(
        project_id="demo",
        instruction="continue old context",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(job.id)

    assert len(runner.started) == 0
    assert runner.resumed[0]["session_id"] == "legacy-thread"
    persistent = await project_sessions.active_for("demo", "100")
    assert persistent is not None
    assert persistent.external_session_id == "legacy-thread"


@pytest.mark.asyncio
async def test_inflight_legacy_job_lazily_creates_project_session(database):
    events, _, project_sessions = _registries(database)

    bound = await project_sessions.bind_for_job(
        project_id="demo",
        owner_user_id="100",
        job_id="JOB-INFLIGHT",
        external_session_id="thread-inflight",
        host_id="lightsail-main",
    )

    assert bound.status == "ACTIVE"
    assert bound.locked_by_job_id == "JOB-INFLIGHT"
    assert bound.external_session_id == "thread-inflight"



@pytest.mark.asyncio
async def test_old_project_session_can_be_reactivated_and_resumed(project_registry, database):
    events, sessions, project_sessions = _registries(database)
    runner = FakeAgentRunner(delay=0.01, resume_session_id="fake-session")
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    first = await manager.create(
        project_id="demo",
        instruction="first context",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(first.id)
    old_session = await project_sessions.active_for("demo", "100")
    assert old_session is not None
    assert old_session.external_session_id == "fake-session"

    await controller.handle_text(
        "/session new",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    replacement = await project_sessions.active_for("demo", "100")
    assert replacement is not None
    assert replacement.id != old_session.id

    response = await controller.handle_text(
        f"/session use {old_session.id}",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert "다시 활성화" in response

    active = await project_sessions.active_for("demo", "100")
    assert active is not None
    assert active.id == old_session.id
    assert active.external_session_id == "fake-session"
    closed_replacement = await project_sessions.get(replacement.id)
    assert closed_replacement is not None
    assert closed_replacement.status == "CLOSED"

    resumed_job = await manager.create(
        project_id="demo",
        instruction="return to old context",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await manager.wait_until_idle(resumed_job.id)

    assert runner.resumed[-1]["session_id"] == "fake-session"
    assert "return to old context" in runner.resumed[-1]["instruction"]


class _AckCancelHandle(RunHandle):
    def __init__(self) -> None:
        self.pid = 4242
        self.session_id = "thread-cancel-ack"
        self.execution_id = None
        self.cancel_requested = asyncio.Event()
        self.cancel_ack = asyncio.Event()
        self.finished = asyncio.Event()

    async def wait(self) -> AgentRunResult:
        await self.finished.wait()
        return AgentRunResult(
            returncode=130,
            session_id=self.session_id,
            final_message="cancelled after ack",
        )

    async def cancel(self) -> None:
        self.cancel_requested.set()
        await self.cancel_ack.wait()
        self.finished.set()


class _AckCancelRunner(AgentRunner):
    def __init__(self) -> None:
        self.handle = _AckCancelHandle()

    async def start(
        self,
        *,
        project_id,
        instruction,
        working_directory,
        host_id=None,
        on_event=None,
    ) -> RunHandle:
        del project_id, instruction, working_directory, host_id
        if on_event is not None:
            await on_event(
                {"type": "thread.started", "thread_id": self.handle.session_id}
            )
        return self.handle


@pytest.mark.asyncio
async def test_project_session_stays_locked_until_cancel_is_confirmed(
    project_registry,
    database,
):
    events, sessions, project_sessions = _registries(database)
    runner = _AckCancelRunner()
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=sessions,
        project_sessions=project_sessions,
    )

    job = await manager.create(
        project_id="demo",
        instruction="long running work",
        requested_by_channel="telegram",
        requested_by_user="100",
    )

    for _ in range(50):
        current = await manager.require(job.id)
        if current.state == "RUNNING":
            break
        await asyncio.sleep(0)
    assert current.state == "RUNNING"

    cancel_task = asyncio.create_task(manager.cancel(job.id))
    await runner.handle.cancel_requested.wait()

    cancelling = await manager.require(job.id)
    assert cancelling.state == "CANCELLING"
    persistent = await project_sessions.active_for("demo", "100")
    assert persistent is not None
    assert persistent.locked_by_job_id == job.id

    with pytest.raises(ProjectSessionBusyError):
        await manager.create(
            project_id="demo",
            instruction="must not overlap",
            requested_by_channel="telegram",
            requested_by_user="100",
        )

    runner.handle.cancel_ack.set()
    cancelled = await cancel_task
    assert cancelled.state == "CANCELLED"

    persistent = await project_sessions.active_for("demo", "100")
    assert persistent is not None
    assert persistent.locked_by_job_id is None
