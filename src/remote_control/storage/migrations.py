from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from remote_control.storage.models import Base

Migration = Callable[[AsyncConnection], Awaitable[None]]
CURRENT_SCHEMA_VERSION = 1


async def _baseline_schema(connection: AsyncConnection) -> None:
    await connection.run_sync(Base.metadata.create_all)


MIGRATIONS: dict[int, Migration] = {
    1: _baseline_schema,
}


async def apply_migrations(connection: AsyncConnection) -> int:
    await connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
    )
    result = await connection.execute(
        text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    )
    current = int(result.scalar_one())

    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            "database schema is newer than this Remote Control build: "
            f"database={current} supported={CURRENT_SCHEMA_VERSION}"
        )

    for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise RuntimeError(f"missing database migration for version {version}")
        await migration(connection)
        await connection.execute(
            text(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (:version, :applied_at)"
            ),
            {
                "version": version,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    return CURRENT_SCHEMA_VERSION


async def read_schema_version(connection: AsyncConnection) -> int:
    result = await connection.execute(
        text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    )
    return int(result.scalar_one())
