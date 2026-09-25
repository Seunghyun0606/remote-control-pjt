from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from remote_control.controller.job_manager import JobManager
from remote_control.runner_daemon import RunnerDaemon
from remote_control.runners.fake import FakeAgentRunner
from remote_control.settings import RunnerSettings
from remote_control.storage.repositories import EventRepository, JobRepository


@pytest.mark.asyncio
async def test_sqlite_runtime_pragmas_are_enabled(database):
    async with database.engine.connect() as connection:
        busy_timeout = (
            await connection.execute(text("PRAGMA busy_timeout"))
        ).scalar_one()
        journal_mode = (
            await connection.execute(text("PRAGMA journal_mode"))
        ).scalar_one()

    assert busy_timeout == 5000
    assert str(journal_mode).lower() == "wal"


class _RetrySession:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, record):
        self.record = record

    async def commit(self):
        self.state["attempts"] += 1
        if self.state["attempts"] < 3:
            raise OperationalError(
                "INSERT",
                {},
                Exception("database is locked"),
            )

    async def refresh(self, record):
        self.record = record


class _RetryDb:
    def __init__(self):
        self.state = {"attempts": 0}

    def sessions(self):
        return _RetrySession(self.state)


@pytest.mark.asyncio
async def test_event_repository_retries_transient_sqlite_busy(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(
        "remote_control.storage.repositories.asyncio.sleep",
        no_sleep,
    )
    db = _RetryDb()
    repository = EventRepository(db)

    record = await repository.append(
        "TEST_EVENT",
        job_id="JOB-1",
        payload={"ok": True},
    )

    assert record.event_type == "TEST_EVENT"
    assert db.state["attempts"] == 3


class _FailAgentEventRepository:
    def __init__(self, delegate):
        self.delegate = delegate
        self.agent_event_failures = 0

    async def append(self, event_type, **kwargs):
        if event_type == "AGENT_EVENT":
            self.agent_event_failures += 1
            raise RuntimeError("telemetry write failed")
        return await self.delegate.append(event_type, **kwargs)


@pytest.mark.asyncio
async def test_agent_event_storage_failure_does_not_fail_job(
    project_registry,
    database,
):
    real_events = EventRepository(database)
    events = _FailAgentEventRepository(real_events)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(delay=0.01),
        local_host_id="lightsail-main",
        progress_interval_seconds=0,
    )

    job = await manager.create(
        project_id="demo",
        instruction="complete despite telemetry failure",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await manager.wait_until_idle(job.id)

    current = await manager.require(job.id)
    assert current.state == "COMPLETED"
    assert events.agent_event_failures >= 1


@pytest.mark.asyncio
async def test_runner_daemon_reconnects_after_unexpected_connection_error():
    settings = RunnerSettings(
        _env_file=None,
        REMOTE_RUNNER_RECONNECT_SECONDS=0,
    )
    daemon = RunnerDaemon(settings)
    calls = 0
    second_connection = asyncio.Event()
    blocker = asyncio.Event()

    async def fake_connection():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected protocol failure")
        second_connection.set()
        await blocker.wait()

    daemon._run_connection = fake_connection
    task = asyncio.create_task(daemon.run_forever())
    await asyncio.wait_for(second_connection.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 2


@pytest.mark.asyncio
async def test_runner_background_task_exception_is_consumed(caplog):
    settings = RunnerSettings(_env_file=None)
    daemon = RunnerDaemon(settings)

    async def fail():
        raise RuntimeError("background boom")

    with caplog.at_level(logging.ERROR):
        task = daemon._start_background_task(
            fail(),
            name="test-background",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert task.done()
    assert task not in daemon._background_tasks
    assert "runner background task failed" in caplog.text
