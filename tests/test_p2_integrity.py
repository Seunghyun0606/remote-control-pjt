from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from remote_control.controller.job_manager import JobManager
from remote_control.event_payloads import sanitize_agent_event
from remote_control.messaging.telegram import TelegramProvider, _PendingSteer
from remote_control.runners.fake import FakeAgentRunner
from remote_control.sessions.project_sessions import ProjectSessionRegistry
from remote_control.storage.db import Database
from remote_control.storage.migrations import CURRENT_SCHEMA_VERSION
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    ProjectSessionRepository,
)


@pytest.mark.asyncio
async def test_database_migration_baseline_is_recorded_and_idempotent(database):
    async with database.engine.connect() as connection:
        version = (
            await connection.execute(
                text("SELECT MAX(version) FROM schema_migrations")
            )
        ).scalar_one()

    assert version == CURRENT_SCHEMA_VERSION

    await database.init()

    async with database.engine.connect() as connection:
        count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            )
        ).scalar_one()
        version = (
            await connection.execute(
                text("SELECT MAX(version) FROM schema_migrations")
            )
        ).scalar_one()

    assert count == CURRENT_SCHEMA_VERSION
    assert version == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migration_baseline_rejects_incomplete_existing_schema(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="database schema drift detected"):
            await database.init()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_migrates_v2_runtime_to_v4(database):
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP TABLE remote_executions"))
        await connection.execute(
            text("ALTER TABLE hosts DROP COLUMN runner_instance_id")
        )
        await connection.execute(
            text("ALTER TABLE hosts DROP COLUMN runner_boot_id")
        )
        await connection.execute(
            text("DELETE FROM schema_migrations WHERE version > 2")
        )

    await database.init()

    async with database.engine.connect() as connection:
        version = (
            await connection.execute(
                text("SELECT MAX(version) FROM schema_migrations")
            )
        ).scalar_one()
        host_columns = {
            row[1]
            for row in (
                await connection.execute(text("PRAGMA table_info(hosts)"))
            ).all()
        }
        tables = {
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table'"
                    )
                )
            ).all()
        }

    assert version == 4
    assert {"runner_instance_id", "runner_boot_id"} <= host_columns
    assert "remote_executions" in tables


@pytest.mark.asyncio
async def test_database_startup_rejects_missing_expected_index(database):
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP INDEX ix_jobs_project_id"))

    with pytest.raises(RuntimeError, match="indexes="):
        await database.init()


@pytest.mark.asyncio
async def test_database_startup_rejects_unexpected_column(database):
    async with database.engine.begin() as connection:
        await connection.execute(text("ALTER TABLE jobs ADD COLUMN rogue TEXT"))

    with pytest.raises(RuntimeError, match="unexpected_columns"):
        await database.init()


@pytest.mark.asyncio
async def test_startup_releases_project_session_lock_for_missing_job(
    project_registry,
    database,
):
    events = EventRepository(database)
    project_sessions = ProjectSessionRegistry(
        sessions=ProjectSessionRepository(database),
        events=events,
    )
    session = await project_sessions.acquire(
        project_id="demo",
        owner_user_id="100",
        job_id="JOB-MISSING",
    )
    assert session.locked_by_job_id == "JOB-MISSING"

    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        project_sessions=project_sessions,
    )

    reconciled = await manager.reconcile_startup()
    current = await project_sessions.get(session.id)

    assert reconciled == 1
    assert current is not None
    assert current.locked_by_job_id is None
    assert current.status == "IDLE"


@pytest.mark.asyncio
async def test_startup_preserves_project_session_lock_for_active_job(
    project_registry,
    database,
):
    events = EventRepository(database)
    jobs = JobRepository(database)
    project_sessions = ProjectSessionRegistry(
        sessions=ProjectSessionRepository(database),
        events=events,
    )
    await jobs.add(
        JobRecord(
            id="JOB-ACTIVE",
            project_id="demo",
            requested_by_channel="test",
            requested_by_user="100",
            requested_host="lightsail-main",
            assigned_host="lightsail-main",
            instruction="active",
            state="WAITING_HOST",
        )
    )
    session = await project_sessions.acquire(
        project_id="demo",
        owner_user_id="100",
        job_id="JOB-ACTIVE",
    )

    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        project_sessions=project_sessions,
    )

    reconciled = await manager.reconcile_startup()
    current = await project_sessions.get(session.id)

    assert reconciled == 0
    assert current is not None
    assert current.locked_by_job_id == "JOB-ACTIVE"


def test_shared_event_sanitizer_bounds_nested_payloads():
    event = {
        "type": "item.completed",
        "message": "m" * 5000,
        "question": "q" * 5000,
        "options": [{"key": "A", "label": "l" * 2000}] * 20,
        "item": {
            "type": "command_execution",
            "command": "c" * 5000,
            "text": "t" * 5000,
            "options": [{"key": "A", "label": "x" * 2000}] * 20,
        },
    }

    cleaned = sanitize_agent_event(event)

    assert len(cleaned["message"]) == 1200
    assert len(cleaned["question"]) == 2000
    assert len(cleaned["options"]) == 8
    assert len(cleaned["options"][0]["label"]) == 500
    assert len(cleaned["item"]["command"]) == 400
    assert len(cleaned["item"]["text"]) == 1200
    assert len(cleaned["item"]["options"]) == 8


class _Query:
    def __init__(self, data: str):
        self.data = data
        self.message = None
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


@pytest.mark.asyncio
async def test_telegram_selection_wrong_user_does_not_consume_token():
    provider = TelegramProvider.__new__(TelegramProvider)
    provider.allowed_user_ids = {100, 200}
    provider.selection_ttl_seconds = 300
    token = "owner-token"
    provider._pending_steers = {
        token: _PendingSteer(
            user_id="100",
            project_id="demo",
            instruction="continue",
            chat_id="100",
            thread_id=42,
            created_at=time.monotonic(),
        )
    }
    query = _Query(f"jobselect:{token}:JOB-1")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=200),
    )

    await provider._handle_job_selection(update, None)

    assert token in provider._pending_steers
    assert query.answers[-1][0] == "다른 사용자의 선택입니다."


@pytest.mark.asyncio
async def test_telegram_selection_expired_token_is_removed(monkeypatch):
    provider = TelegramProvider.__new__(TelegramProvider)
    provider.allowed_user_ids = {100}
    provider.selection_ttl_seconds = 10
    token = "expired-token"
    provider._pending_steers = {
        token: _PendingSteer(
            user_id="100",
            project_id="demo",
            instruction="continue",
            chat_id="100",
            thread_id=42,
            created_at=100.0,
        )
    }
    monkeypatch.setattr(
        "remote_control.messaging.telegram.time.monotonic",
        lambda: 111.0,
    )
    query = _Query(f"jobselect:{token}:JOB-1")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=100),
    )

    await provider._handle_job_selection(update, None)

    assert token not in provider._pending_steers
    assert query.answers[-1][0] == "선택 시간이 만료되었습니다."
