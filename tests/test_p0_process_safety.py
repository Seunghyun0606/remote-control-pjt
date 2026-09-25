from __future__ import annotations

import asyncio
import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.execution_leases import ExecutionLeaseRegistry
from remote_control.recovery.models import RecoveryMode
from remote_control.runner_daemon import RunnerDaemon
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
