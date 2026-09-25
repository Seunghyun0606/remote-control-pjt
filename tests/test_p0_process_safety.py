from __future__ import annotations

import asyncio
import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.execution_leases import ExecutionLeaseRegistry
from remote_control.process_control import ProcessSafetyError
from remote_control.projects.models import ProjectDefinition, RepositoryConfig
from remote_control.projects.registry import ProjectRegistry
from remote_control.recovery.models import RecoveryKind, RecoveryMode
from remote_control.runner_daemon import RunnerDaemon, RunnerSafetyError
from remote_control.runner_journal import RunnerExecutionJournal
from remote_control.runners.fake import FakeAgentRunner
from remote_control.settings import RunnerSettings
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import (
    EventRepository,
    ExecutionLeaseRepository,
    JobRepository,
    RecoveryRepository,
)
from remote_control.transport.protocol import message
from remote_control.transport.runner_ws import RunnerGateway


async def _wait_for_state(manager: JobManager, job_id: str, state: str) -> None:
    for _ in range(200):
        current = await manager.require(job_id)
        if current.state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {state}")


def _lease_registry(database) -> ExecutionLeaseRegistry:
    events = EventRepository(database)
    return ExecutionLeaseRegistry(
        leases=ExecutionLeaseRepository(database),
        events=events,
    )


@pytest.mark.asyncio
async def test_same_working_tree_is_exclusive_across_users_and_channels(
    project_registry,
    database,
):
    events = EventRepository(database)
    leases = _lease_registry(database)
    runner = FakeAgentRunner(delay=1)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        execution_leases=leases,
        recovery=RecoveryRepository(database),
    )

    first = await manager.create(
        project_id="demo",
        instruction="telegram work",
        requested_by_channel="telegram",
        requested_by_user="telegram-user",
    )
    await _wait_for_state(manager, first.id, "RUNNING")

    second = await manager.create(
        project_id="demo",
        instruction="slack work",
        requested_by_channel="slack",
        requested_by_user="slack-user",
    )
    second = await manager.require(second.id)

    assert second.state == "FAILED"
    assert "already leased by job" in (second.error or "")
    current_lease = await leases.get_for_job(first.id)
    assert current_lease is not None
    assert await leases.get_for_job(second.id) is None

    await manager.cancel(first.id)
    assert (await manager.require(first.id)).state == "CANCELLED"
    assert await leases.get_for_job(first.id) is None

    third = await manager.create(
        project_id="demo",
        instruction="slack work after release",
        requested_by_channel="slack",
        requested_by_user="slack-user",
    )
    await manager.wait_until_idle(third.id)
    assert (await manager.require(third.id)).state == "COMPLETED"


@pytest.mark.asyncio
async def test_local_process_safety_error_holds_lease_until_pid_is_reconciled(
    monkeypatch,
    project_registry,
    database,
):
    events = EventRepository(database)
    leases = _lease_registry(database)
    runner = FakeAgentRunner()

    async def unsafe_start(**_kwargs):
        raise ProcessSafetyError(
            "spawned Codex could not be terminated",
            pid=7777,
        )

    runner.start = unsafe_start
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        execution_leases=leases,
        recovery=RecoveryRepository(database),
    )

    job = await manager.create(
        project_id="demo",
        instruction="unsafe start",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await _wait_for_state(manager, job.id, "PAUSED")

    held = await manager.require(job.id)
    assert held.pid == 7777
    assert (held.error or "").startswith("PROCESS_SAFETY_HOLD:")
    assert await leases.get_for_job(job.id) is not None

    with pytest.raises(ValueError, match="process safety hold"):
        await manager.resume(job.id)

    async def cannot_terminate(pid, *, working_directory, timeout_seconds):
        del pid, working_directory, timeout_seconds
        return False

    monkeypatch.setattr(
        "remote_control.controller.job_manager.terminate_persisted_codex_process",
        cannot_terminate,
    )
    with pytest.raises(RuntimeError, match="termination is still unconfirmed"):
        await manager.cancel(job.id)

    still_held = await manager.require(job.id)
    assert still_held.state == "PAUSED"
    assert still_held.pid == 7777
    assert await leases.get_for_job(job.id) is not None

    async def terminate(pid, *, working_directory, timeout_seconds):
        del pid, working_directory, timeout_seconds
        return True

    monkeypatch.setattr(
        "remote_control.controller.job_manager.terminate_persisted_codex_process",
        terminate,
    )
    cancelled = await manager.cancel(job.id)
    assert cancelled.state == "CANCELLED"
    assert cancelled.pid is None
    assert await leases.get_for_job(job.id) is None


@pytest.mark.asyncio
async def test_startup_refuses_conflicting_active_jobs_on_same_working_tree(
    project_registry,
    database,
):
    jobs = JobRepository(database)
    for job_id, user in (("JOB-A", "telegram-user"), ("JOB-B", "slack-user")):
        await jobs.add(
            JobRecord(
                id=job_id,
                project_id="demo",
                requested_by_channel="test",
                requested_by_user=user,
                requested_host="lightsail-main",
                assigned_host="lightsail-main",
                instruction="active",
                state="WAITING_HOST",
            )
        )

    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        execution_leases=_lease_registry(database),
        recovery=RecoveryRepository(database),
    )

    with pytest.raises(RuntimeError, match="conflicting active Jobs"):
        await manager.reconcile_startup()


@pytest.mark.asyncio
async def test_controller_shutdown_stops_local_execution_without_unlocking_lease(
    project_registry,
    database,
):
    events = EventRepository(database)
    leases = _lease_registry(database)
    recovery = RecoveryRepository(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(delay=10),
        local_host_id="lightsail-main",
        execution_leases=leases,
        recovery=recovery,
    )

    job = await manager.create(
        project_id="demo",
        instruction="long local work",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await _wait_for_state(manager, job.id, "RUNNING")
    assert await leases.get_for_job(job.id) is not None

    await manager.shutdown()

    current = await manager.require(job.id)
    assert current.state == "WAITING_HOST"
    assert current.pid is None
    assert await leases.get_for_job(job.id) is not None
    record = await recovery.get(job.id)
    assert record is not None
    assert record.mode == RecoveryMode.RESUME.value


@pytest.mark.asyncio
async def test_startup_terminates_persisted_local_process_before_recovery(
    monkeypatch,
    project_registry,
    database,
):
    calls = []

    async def terminate(pid, *, working_directory, timeout_seconds):
        calls.append((pid, str(working_directory), timeout_seconds))
        return True

    monkeypatch.setattr(
        "remote_control.controller.job_manager.terminate_persisted_codex_process",
        terminate,
    )

    jobs = JobRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-LOCAL-CRASH",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="lightsail-main",
            assigned_host="lightsail-main",
            instruction="unfinished",
            state="RUNNING",
            external_session_id="thread-local",
            pid=98765,
        )
    )
    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        recovery=RecoveryRepository(database),
        restart_grace_seconds=0,
    )

    await manager.reconcile_startup()

    current = await manager.require("JOB-LOCAL-CRASH")
    assert calls
    assert calls[0][0] == 98765
    assert calls[0][1] == str(project_registry.get("demo").path_for("lightsail-main"))
    assert current.pid is None
    assert current.state == "WAITING_HOST"


@pytest.mark.asyncio
async def test_runner_restart_terminates_journaled_execution_and_keeps_result_until_ack(
    monkeypatch,
    tmp_path,
):
    stopped = []

    async def terminate(pid, *, working_directory, timeout_seconds):
        stopped.append((pid, working_directory, timeout_seconds))
        return True

    monkeypatch.setattr(
        "remote_control.runner_daemon.terminate_persisted_codex_process",
        terminate,
    )

    path = tmp_path / "runner-journal.json"
    journal = RunnerExecutionJournal(path)
    journal.reserve(
        execution_id="exec-1",
        working_directory="C:/dev/demo",
        boot_id="old-boot",
        session_id="thread-1",
    )
    journal.attach_pid("exec-1", 4242)
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_HOST_ID="desktop-main",
        REMOTE_RUNNER_STATE_PATH=str(path),
    )
    daemon = RunnerDaemon(settings, journal=journal)

    await daemon._recover_persisted_executions()

    assert stopped == [(4242, "C:/dev/demo", 10)]
    assert "exec-1" in daemon.completed
    persisted = journal.get("exec-1")
    assert persisted is not None
    assert persisted.state == "COMPLETED"
    assert persisted.returncode == 130

    await daemon._handle(object(), message("JOB_RESULT_ACK", execution_id="exec-1"))
    assert "exec-1" not in daemon.completed
    assert journal.get("exec-1") is None


@pytest.mark.asyncio
async def test_runner_refuses_startup_when_orphan_identity_cannot_be_proven(
    monkeypatch,
    tmp_path,
):
    async def terminate(pid, *, working_directory, timeout_seconds):
        del pid, working_directory, timeout_seconds
        return False

    monkeypatch.setattr(
        "remote_control.runner_daemon.terminate_persisted_codex_process",
        terminate,
    )
    path = tmp_path / "runner-journal.json"
    journal = RunnerExecutionJournal(path)
    journal.reserve(
        execution_id="exec-unsafe",
        working_directory="C:/dev/demo",
        boot_id="old-boot",
        session_id=None,
    )
    journal.attach_pid("exec-unsafe", 9999)
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_HOST_ID="desktop-main",
        REMOTE_RUNNER_STATE_PATH=str(path),
    )
    daemon = RunnerDaemon(settings, journal=journal)

    with pytest.raises(RuntimeError, match="refusing to start Runner"):
        await daemon._recover_persisted_executions()



@pytest.mark.asyncio
async def test_runner_refuses_uncertain_starting_reservation(tmp_path):
    path = tmp_path / "runner-journal.json"
    journal = RunnerExecutionJournal(path)
    journal.reserve(
        execution_id="exec-starting",
        working_directory="C:/dev/demo",
        boot_id="old-boot",
        session_id=None,
    )
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_HOST_ID="desktop-main",
        REMOTE_RUNNER_STATE_PATH=str(path),
    )
    daemon = RunnerDaemon(settings, journal=journal)

    with pytest.raises(RuntimeError, match="may have spawned before its PID"):
        await daemon._recover_persisted_executions()


@pytest.mark.asyncio
async def test_controller_refuses_active_local_job_without_durable_pid(
    project_registry,
    database,
):
    jobs = JobRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-UNCERTAIN-LOCAL",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="lightsail-main",
            assigned_host="lightsail-main",
            instruction="uncertain",
            state="RUNNING",
            external_session_id="thread-local",
            pid=None,
        )
    )
    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        recovery=RecoveryRepository(database),
    )

    with pytest.raises(RuntimeError, match="PID was not durably recorded"):
        await manager.reconcile_startup()



class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000):
        self.closed.append(code)


@pytest.mark.asyncio
async def test_runner_reconcile_recovers_missing_execution_id_by_latest_working_tree(
    tmp_path,
    database,
):
    projects = ProjectRegistry(
        {
            "demo": ProjectDefinition(
                id="demo",
                name="Demo",
                repository=RepositoryConfig(
                    path={"desktop-main": "C:/dev/demo"}
                ),
                allowed_hosts=["desktop-main"],
                default_host="desktop-main",
            )
        }
    )
    jobs = JobRepository(database)
    recovery = RecoveryRepository(database)
    await jobs.add(
        JobRecord(
            id="JOB-REMOTE-CRASH-WINDOW",
            project_id="demo",
            requested_by_channel="telegram",
            requested_by_user="100",
            requested_host="desktop-main",
            assigned_host="desktop-main",
            instruction="unfinished",
            state="WAITING_HOST",
            external_session_id="thread-current",
        )
    )
    await recovery.upsert(
        "JOB-REMOTE-CRASH-WINDOW",
        kind=RecoveryKind.RESTART.value,
        mode=RecoveryMode.RESUME.value,
        attempt_count=0,
        next_retry_at=None,
        execution_id=None,
        resume_instruction="continue",
        last_error="controller restarted before execution id was persisted",
    )
    manager = JobManager(
        projects=projects,
        jobs=jobs,
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        recovery=recovery,
        execution_leases=_lease_registry(database),
    )
    await manager._acquire_execution_lease(
        "JOB-REMOTE-CRASH-WINDOW",
        "desktop-main",
    )

    gateway = RunnerGateway()
    websocket = _FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    adopted = await manager.reconcile_runner(
        host_id="desktop-main",
        running_jobs=[
            {
                "execution_id": "exec-current",
                "session_id": "thread-current",
                "working_directory": "C:/dev/demo",
                "started_at": "2026-09-26T10:00:00+00:00",
            }
        ],
        completed_jobs=[
            {
                "execution_id": "exec-old",
                "session_id": "thread-old",
                "working_directory": "C:/dev/demo",
                "started_at": "2026-09-25T10:00:00+00:00",
            }
        ],
        gateway=gateway,
    )

    assert adopted == 1
    rebound = await recovery.get("JOB-REMOTE-CRASH-WINDOW")
    assert rebound is not None
    assert rebound.execution_id == "exec-current"
    assert (await manager.require("JOB-REMOTE-CRASH-WINDOW")).state == "RUNNING"

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id="exec-current",
            returncode=0,
            session_id="thread-current",
            final_message="done",
        ),
    )
    await _wait_for_state(manager, "JOB-REMOTE-CRASH-WINDOW", "COMPLETED")
    for _ in range(100):
        if websocket.sent:
            break
        await asyncio.sleep(0.01)
    assert websocket.sent
    from remote_control.transport.protocol import Envelope
    ack = Envelope.model_validate_json(websocket.sent[-1])
    assert ack.type == "JOB_RESULT_ACK"
    assert ack.payload["execution_id"] == "exec-current"



@pytest.mark.asyncio
async def test_runner_safety_error_stops_reconnect_loop(tmp_path):
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_RECONNECT_SECONDS=0,
        REMOTE_RUNNER_STATE_PATH=str(tmp_path / "journal.json"),
    )
    daemon = RunnerDaemon(settings)

    async def fail_connection():
        raise RunnerSafetyError("journal safety failed")

    daemon._run_connection = fail_connection

    with pytest.raises(RunnerSafetyError, match="journal safety failed"):
        await daemon.run_forever()
