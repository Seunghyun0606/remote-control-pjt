from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from remote_control.controller.job_manager import JobManager
from remote_control.controller_lock import (
    ControllerAlreadyRunningError,
    ControllerRuntimeLock,
    RunnerAlreadyRunningError,
    RunnerRuntimeLock,
)
from remote_control.execution_leases import ExecutionLeaseRegistry
from remote_control.hosts.registry import HostRegistry
from remote_control.projects.models import ProjectDefinition, RepositoryConfig
from remote_control.projects.registry import ProjectRegistry
from remote_control.recovery.models import RecoveryKind, RecoveryMode
from remote_control.runner_daemon import RunnerDaemon, RunnerSafetyError
from remote_control.runner_journal import RunnerExecutionJournal
from remote_control.runners.base import AgentRunResult
from remote_control.runners.fake import FakeAgentRunner, FakeRunHandle
from remote_control.settings import RunnerSettings
from remote_control.storage.models import EventRecord, JobRecord
from remote_control.storage.repositories import (
    EventRepository,
    ExecutionLeaseRepository,
    HostRepository,
    JobRepository,
    RecoveryRepository,
    RemoteExecutionRepository,
)
from remote_control.transport.protocol import Envelope
from remote_control.transport.runner_ws import RunnerGateway, RunnerInstanceConflict


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[int] = []

    async def send_text(self, value: str) -> None:
        self.sent.append(value)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def _remote_projects(tmp_path: Path) -> ProjectRegistry:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    return ProjectRegistry(
        {
            "demo": ProjectDefinition(
                id="demo",
                name="Demo",
                adapter="generic_git",
                repository=RepositoryConfig(
                    path={
                        "lightsail-main": str(local),
                        "desktop-main": str(remote),
                    }
                ),
                allowed_hosts=["lightsail-main", "desktop-main"],
                default_host="desktop-main",
            )
        }
    )


def test_controller_runtime_lock_rejects_second_process_owner(tmp_path: Path):
    path = tmp_path / "controller.lock"
    first = ControllerRuntimeLock(path)
    second = ControllerRuntimeLock(path)

    first.acquire()
    try:
        with pytest.raises(ControllerAlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_runner_runtime_lock_rejects_second_process_owner(tmp_path: Path):
    path = tmp_path / "runner.json.lock"
    first = RunnerRuntimeLock(path)
    second = RunnerRuntimeLock(path)

    first.acquire()
    try:
        with pytest.raises(RunnerAlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_runner_daemon_locks_before_journal_initialization(tmp_path: Path):
    state_path = tmp_path / "runner.json"
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_STATE_PATH=str(state_path),
    )

    first = RunnerDaemon(settings)
    try:
        first_instance_id = first.instance_id
        persisted_before = state_path.read_text(encoding="utf-8")

        with pytest.raises(RunnerAlreadyRunningError):
            RunnerDaemon(settings)

        assert state_path.read_text(encoding="utf-8") == persisted_before
        assert RunnerExecutionJournal(state_path).instance_id == first_instance_id
    finally:
        first.runtime_lock.release()


def test_runner_journal_instance_identity_survives_restart(tmp_path: Path):
    path = tmp_path / "runner.json"

    first = RunnerExecutionJournal(path)
    first_id = first.instance_id
    second = RunnerExecutionJournal(path)

    assert first_id
    assert second.instance_id == first_id


@pytest.mark.asyncio
async def test_gateway_rejects_different_live_runner_instance():
    gateway = RunnerGateway()
    old = _FakeWebSocket()
    replacement = _FakeWebSocket()

    await gateway.attach(
        "desktop-main",
        old,
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )

    with pytest.raises(RunnerInstanceConflict):
        await gateway.attach(
            "desktop-main",
            replacement,
            runner_instance_id="runner-B",
            runner_boot_id="boot-B",
        )

    assert old.closed == []
    assert gateway.runner_instance_id("desktop-main") == "runner-A"


@pytest.mark.asyncio
async def test_gateway_rejects_same_instance_different_live_boot():
    gateway = RunnerGateway()
    old = _FakeWebSocket()
    replacement = _FakeWebSocket()

    await gateway.attach(
        "desktop-main",
        old,
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )

    with pytest.raises(RunnerInstanceConflict):
        await gateway.attach(
            "desktop-main",
            replacement,
            runner_instance_id="runner-A",
            runner_boot_id="boot-B",
        )

    assert old.closed == []
    assert gateway.runner_boot_id("desktop-main") == "boot-A"


@pytest.mark.asyncio
async def test_gateway_same_boot_reconnect_keeps_stale_detach_safe():
    gateway = RunnerGateway()
    old = _FakeWebSocket()
    replacement = _FakeWebSocket()

    await gateway.attach(
        "desktop-main",
        old,
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )
    await gateway.attach(
        "desktop-main",
        replacement,
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )

    assert old.closed == [1012]
    assert await gateway.detach("desktop-main", old) is False
    assert gateway.is_connected("desktop-main") is True


@pytest.mark.asyncio
async def test_runner_instance_takeover_is_blocked_while_host_has_active_job(database):
    events = EventRepository(database)
    hosts = HostRegistry(
        hosts=HostRepository(database),
        events=events,
        local_host_id="lightsail-main",
    )
    await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex"},
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )
    jobs = JobRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-ACTIVE",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="desktop-main",
            assigned_host="desktop-main",
            instruction="active",
            state="WAITING_HOST",
        )
    )
    manager = JobManager(
        projects=ProjectRegistry({}),
        jobs=jobs,
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        hosts=hosts,
    )

    with pytest.raises(ValueError, match="takeover is unsafe"):
        await manager.validate_runner_registration(
            host_id="desktop-main",
            runner_instance_id="runner-B",
        )


@pytest.mark.asyncio
async def test_terminal_remote_result_is_acknowledged_by_exact_ownership(
    tmp_path: Path,
    database,
):
    projects = _remote_projects(tmp_path)
    events = EventRepository(database)
    jobs = JobRepository(database)
    hosts = HostRegistry(
        hosts=HostRepository(database),
        events=events,
        local_host_id="lightsail-main",
    )
    await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex"},
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )
    await jobs.add(
        JobRecord(
            id="JOB-DONE",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="desktop-main",
            assigned_host="desktop-main",
            instruction="done",
            state="COMPLETED",
            result="done",
        )
    )
    ownership = RemoteExecutionRepository(database)
    await ownership.upsert(
        "exec-done",
        job_id="JOB-DONE",
        host_id="desktop-main",
        runner_instance_id="runner-A",
        working_directory=str(projects.get("demo").path_for("desktop-main")),
        session_id="thread-done",
        state="RESULT_RECEIVED",
        started_at=datetime.now(timezone.utc),
    )
    manager = JobManager(
        projects=projects,
        jobs=jobs,
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        hosts=hosts,
        recovery=RecoveryRepository(database),
        remote_executions=ownership,
    )
    gateway = RunnerGateway()
    websocket = _FakeWebSocket()
    await gateway.attach(
        "desktop-main",
        websocket,
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )

    adopted = await manager.reconcile_runner(
        host_id="desktop-main",
        running_jobs=[],
        completed_jobs=[
            {
                "execution_id": "exec-done",
                "session_id": "thread-done",
                "working_directory": str(
                    projects.get("demo").path_for("desktop-main")
                ),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
        gateway=gateway,
        runner_instance_id="runner-A",
    )

    assert adopted == 0
    ack = Envelope.model_validate_json(websocket.sent[-1])
    assert ack.type == "JOB_RESULT_ACK"
    assert ack.payload["execution_id"] == "exec-done"
    record = await ownership.get("exec-done")
    assert record is not None
    assert record.state == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_old_owned_result_cannot_be_rebound_to_new_job(tmp_path: Path, database):
    projects = _remote_projects(tmp_path)
    events = EventRepository(database)
    jobs = JobRepository(database)
    recovery = RecoveryRepository(database)
    ownership = RemoteExecutionRepository(database)
    hosts = HostRegistry(
        hosts=HostRepository(database),
        events=events,
        local_host_id="lightsail-main",
    )
    await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex"},
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )

    await jobs.add(
        JobRecord(
            id="JOB-OLD",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="old",
            requested_host="desktop-main",
            assigned_host="desktop-main",
            instruction="old",
            state="COMPLETED",
        )
    )
    await jobs.add(
        JobRecord(
            id="JOB-NEW",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="new",
            requested_host="desktop-main",
            assigned_host="desktop-main",
            instruction="new",
            state="WAITING_HOST",
        )
    )
    await recovery.upsert(
        "JOB-NEW",
        kind=RecoveryKind.RESTART.value,
        mode=RecoveryMode.ADOPT.value,
        attempt_count=0,
        next_retry_at=None,
        execution_id=None,
        resume_instruction="continue",
        last_error="controller restarted",
    )
    await ownership.upsert(
        "exec-old",
        job_id="JOB-OLD",
        host_id="desktop-main",
        runner_instance_id="runner-A",
        working_directory=str(projects.get("demo").path_for("desktop-main")),
        session_id="thread-old",
        state="RESULT_RECEIVED",
        started_at=datetime.now(timezone.utc),
    )

    manager = JobManager(
        projects=projects,
        jobs=jobs,
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        hosts=hosts,
        recovery=recovery,
        remote_executions=ownership,
    )
    gateway = RunnerGateway()
    websocket = _FakeWebSocket()
    await gateway.attach(
        "desktop-main",
        websocket,
        runner_instance_id="runner-A",
        runner_boot_id="boot-A",
    )

    adopted = await manager.reconcile_runner(
        host_id="desktop-main",
        running_jobs=[],
        completed_jobs=[
            {
                "execution_id": "exec-old",
                "session_id": "thread-old",
                "working_directory": str(
                    projects.get("demo").path_for("desktop-main")
                ),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
        gateway=gateway,
        runner_instance_id="runner-A",
    )

    assert adopted == 0
    assert (await manager.require("JOB-NEW")).state == "WAITING_HOST"
    updated = await recovery.get("JOB-NEW")
    assert updated is not None
    assert updated.mode == RecoveryMode.START.value
    assert updated.execution_id is None


@pytest.mark.asyncio
async def test_queued_orphan_lease_is_released_and_job_becomes_recoverable(
    project_registry,
    database,
):
    events = EventRepository(database)
    jobs = JobRepository(database)
    recovery = RecoveryRepository(database)
    leases = ExecutionLeaseRegistry(
        leases=ExecutionLeaseRepository(database),
        events=events,
    )
    await jobs.add(
        JobRecord(
            id="JOB-ORPHAN-LEASE",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="lightsail-main",
            assigned_host=None,
            instruction="queued",
            state="QUEUED",
        )
    )
    await leases.acquire(
        job_id="JOB-ORPHAN-LEASE",
        project_id="demo",
        host_id="lightsail-main",
        working_directory=project_registry.get("demo").path_for("lightsail-main"),
    )

    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        recovery=recovery,
        execution_leases=leases,
    )

    await manager.reconcile_startup()

    job = await manager.require("JOB-ORPHAN-LEASE")
    assert job.state == "WAITING_HOST"
    assert await leases.get_for_job(job.id) is None
    record = await recovery.get(job.id)
    assert record is not None
    assert record.mode == RecoveryMode.START.value


@pytest.mark.asyncio
async def test_atomic_assignment_survives_audit_event_failure(
    monkeypatch,
    project_registry,
    database,
):
    events = EventRepository(database)
    jobs = JobRepository(database)
    leases = ExecutionLeaseRegistry(
        leases=ExecutionLeaseRepository(database),
        events=events,
    )
    await jobs.add(
        JobRecord(
            id="JOB-ATOMIC",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="lightsail-main",
            assigned_host=None,
            instruction="assign",
            state="QUEUED",
        )
    )

    async def fail_event(*_args, **_kwargs):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(events, "append", fail_event)

    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        execution_leases=leases,
    )
    assigned = await manager._assign_execution_lease(
        "JOB-ATOMIC",
        "lightsail-main",
    )

    assert assigned.state == "ASSIGNED"
    assert assigned.assigned_host == "lightsail-main"
    assert await leases.get_for_job("JOB-ATOMIC") is not None
    async with database.sessions() as session:
        result = await session.execute(
            select(EventRecord).where(
                EventRecord.job_id == "JOB-ATOMIC",
                EventRecord.event_type == "JOB_ASSIGNED",
            )
        )
        events = list(result.scalars())
    assert len(events) == 1


@pytest.mark.asyncio
async def test_job_lifecycle_transition_and_event_commit_atomically(database):
    jobs = JobRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-LIFECYCLE-ATOMIC",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="auto",
            assigned_host=None,
            instruction="atomic",
            state="QUEUED",
        )
    )

    updated = await jobs.transition_with_event(
        "JOB-LIFECYCLE-ATOMIC",
        expected_state="QUEUED",
        target_state="WAITING_HOST",
        changes={"error": "host unavailable"},
        event_type="JOB_WAITING_HOST",
        payload={"from": "QUEUED", "to": "WAITING_HOST"},
    )

    assert updated.state == "WAITING_HOST"
    assert updated.error == "host unavailable"
    async with database.sessions() as session:
        result = await session.execute(
            select(EventRecord).where(
                EventRecord.job_id == "JOB-LIFECYCLE-ATOMIC",
                EventRecord.event_type == "JOB_WAITING_HOST",
            )
        )
        events = list(result.scalars())
    assert len(events) == 1


@pytest.mark.asyncio
async def test_job_lifecycle_transition_rolls_back_when_event_insert_fails(database):
    jobs = JobRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-LIFECYCLE-ROLLBACK",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="auto",
            assigned_host=None,
            instruction="rollback",
            state="QUEUED",
        )
    )

    with pytest.raises(IntegrityError):
        await jobs.transition_with_event(
            "JOB-LIFECYCLE-ROLLBACK",
            expected_state="QUEUED",
            target_state="WAITING_HOST",
            changes={"error": "must rollback"},
            event_type=None,
            payload={"from": "QUEUED", "to": "WAITING_HOST"},
        )

    persisted = await jobs.get("JOB-LIFECYCLE-ROLLBACK")
    assert persisted is not None
    assert persisted.state == "QUEUED"
    assert persisted.error is None


@pytest.mark.asyncio
async def test_job_creation_rolls_back_when_lifecycle_event_insert_fails(database):
    jobs = JobRepository(database)
    job = JobRecord(
        id="JOB-CREATE-ROLLBACK",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="100",
        requested_host="auto",
        assigned_host=None,
        instruction="rollback create",
        state="QUEUED",
    )

    with pytest.raises(IntegrityError):
        await jobs.add_with_event(
            job,
            event_type=None,
            payload={"requested_host": "auto"},
        )

    assert await jobs.get("JOB-CREATE-ROLLBACK") is None


@pytest.mark.asyncio
async def test_runner_terminal_journal_failure_is_safety_fatal(
    monkeypatch,
    tmp_path: Path,
):
    journal = RunnerExecutionJournal(tmp_path / "runner.json")
    journal.reserve(
        execution_id="exec-1",
        working_directory=str(tmp_path),
        boot_id="boot-old",
        session_id="thread-1",
    )
    journal.attach_pid("exec-1", 4242)
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_STATE_PATH=str(tmp_path / "runner.json"),
    )
    daemon = RunnerDaemon(settings, journal=journal)
    handle = FakeRunHandle(
        result=AgentRunResult(
            returncode=0,
            session_id="thread-1",
            final_message="done",
        ),
        execution_id="exec-1",
    )
    daemon.running["exec-1"] = handle
    daemon.running_sessions["exec-1"] = "thread-1"
    daemon.running_working_directories["exec-1"] = str(tmp_path)

    def fail_complete(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "complete", fail_complete)

    with pytest.raises(RunnerSafetyError, match="durably persist"):
        await daemon._finish_job("exec-1", handle)

    assert "exec-1" in daemon.running
    assert "exec-1" not in daemon.completed
