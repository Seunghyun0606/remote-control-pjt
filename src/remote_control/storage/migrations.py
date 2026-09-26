from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from remote_control.storage.models import Base, ExecutionLeaseRecord, RemoteExecutionRecord
from remote_control.storage.schema_v1 import V1_METADATA

Migration = Callable[[AsyncConnection], Awaitable[None]]
CURRENT_SCHEMA_VERSION = 6


async def _baseline_schema(connection: AsyncConnection) -> None:
    await connection.run_sync(V1_METADATA.create_all)


async def _execution_leases(connection: AsyncConnection) -> None:
    await connection.run_sync(
        lambda sync_connection: ExecutionLeaseRecord.__table__.create(
            sync_connection,
            checkfirst=True,
        )
    )


async def _runner_identity(connection: AsyncConnection) -> None:
    def migrate(sync_connection) -> None:
        inspector = inspect(sync_connection)
        columns = {column["name"] for column in inspector.get_columns("hosts")}
        if "runner_instance_id" not in columns:
            sync_connection.execute(
                text("ALTER TABLE hosts ADD COLUMN runner_instance_id VARCHAR(64)")
            )
        if "runner_boot_id" not in columns:
            sync_connection.execute(
                text("ALTER TABLE hosts ADD COLUMN runner_boot_id VARCHAR(64)")
            )

    await connection.run_sync(migrate)


async def _remote_executions(connection: AsyncConnection) -> None:
    await connection.run_sync(
        lambda sync_connection: RemoteExecutionRecord.__table__.create(
            sync_connection,
            checkfirst=True,
        )
    )


async def _lease_queue_position(connection: AsyncConnection) -> None:
    def migrate(sync_connection) -> None:
        inspector = inspect(sync_connection)
        columns = {column["name"] for column in inspector.get_columns("recovery")}
        if "queue_position" not in columns:
            sync_connection.execute(
                text("ALTER TABLE recovery ADD COLUMN queue_position INTEGER")
            )

    await connection.run_sync(migrate)


async def _process_identity(connection: AsyncConnection) -> None:
    def migrate(sync_connection) -> None:
        inspector = inspect(sync_connection)
        columns = {column["name"] for column in inspector.get_columns("jobs")}
        if "process_executable" not in columns:
            sync_connection.execute(
                text("ALTER TABLE jobs ADD COLUMN process_executable TEXT")
            )
        if "process_start_token" not in columns:
            sync_connection.execute(
                text("ALTER TABLE jobs ADD COLUMN process_start_token VARCHAR(128)")
            )

    await connection.run_sync(migrate)


MIGRATIONS: dict[int, Migration] = {
    1: _baseline_schema,
    2: _execution_leases,
    3: _runner_identity,
    4: _remote_executions,
    5: _process_identity,
    6: _lease_queue_position,
}


def _type_signature(column_type) -> tuple[str, int | None]:
    if isinstance(column_type, Text):
        return ("TEXT", None)
    if isinstance(column_type, String):
        return ("STRING", column_type.length)
    if isinstance(column_type, Integer):
        return ("INTEGER", None)
    if isinstance(column_type, DateTime):
        return ("DATETIME", None)
    return (column_type.__class__.__name__.upper(), None)


def _expected_indexes(table) -> set[tuple[tuple[str, ...], bool]]:
    return {
        (tuple(column.name for column in index.columns), bool(index.unique))
        for index in table.indexes
    }


def _actual_indexes(inspector, table_name: str) -> set[tuple[tuple[str, ...], bool]]:
    return {
        (
            tuple(index.get("column_names") or ()),
            bool(index.get("unique")),
        )
        for index in inspector.get_indexes(table_name)
    }


def _expected_unique_constraints(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _actual_unique_constraints(inspector, table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(table_name)
    }


def _validate_schema_sync(sync_connection) -> None:
    inspector = inspect(sync_connection)
    expected_tables = {table.name for table in Base.metadata.sorted_tables}
    actual_tables = set(inspector.get_table_names()) - {"schema_migrations"}

    missing_tables = sorted(expected_tables - actual_tables)
    unexpected_tables = sorted(actual_tables - expected_tables)
    if missing_tables or unexpected_tables:
        raise RuntimeError(
            "database schema drift detected: "
            f"missing_tables={missing_tables} unexpected_tables={unexpected_tables}"
        )

    for table in Base.metadata.sorted_tables:
        actual_columns = {
            column["name"]: column
            for column in inspector.get_columns(table.name)
        }
        expected_columns = {column.name: column for column in table.columns}

        missing_columns = sorted(set(expected_columns) - set(actual_columns))
        unexpected_columns = sorted(set(actual_columns) - set(expected_columns))
        if missing_columns or unexpected_columns:
            raise RuntimeError(
                "database schema drift detected: "
                f"table={table.name} missing_columns={missing_columns} "
                f"unexpected_columns={unexpected_columns}"
            )

        primary_key_columns = tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
        expected_primary_key = tuple(column.name for column in table.primary_key.columns)
        if primary_key_columns != expected_primary_key:
            raise RuntimeError(
                "database schema drift detected: "
                f"table={table.name} primary_key={primary_key_columns} "
                f"expected={expected_primary_key}"
            )

        for name, expected in expected_columns.items():
            actual = actual_columns[name]
            actual_type = _type_signature(actual["type"])
            expected_type = _type_signature(expected.type)
            if actual_type != expected_type:
                raise RuntimeError(
                    "database schema drift detected: "
                    f"table={table.name} column={name} type={actual_type} "
                    f"expected={expected_type}"
                )

            if not expected.primary_key:
                actual_nullable = bool(actual.get("nullable", True))
                if actual_nullable != bool(expected.nullable):
                    raise RuntimeError(
                        "database schema drift detected: "
                        f"table={table.name} column={name} "
                        f"nullable={actual_nullable} expected={bool(expected.nullable)}"
                    )

        actual_indexes = _actual_indexes(inspector, table.name)
        expected_indexes = _expected_indexes(table)
        if actual_indexes != expected_indexes:
            raise RuntimeError(
                "database schema drift detected: "
                f"table={table.name} indexes={sorted(actual_indexes)} "
                f"expected={sorted(expected_indexes)}"
            )

        actual_uniques = _actual_unique_constraints(inspector, table.name)
        expected_uniques = _expected_unique_constraints(table)
        if actual_uniques != expected_uniques:
            raise RuntimeError(
                "database schema drift detected: "
                f"table={table.name} unique_constraints={sorted(actual_uniques)} "
                f"expected={sorted(expected_uniques)}"
            )


async def validate_schema(connection: AsyncConnection) -> None:
    await connection.run_sync(_validate_schema_sync)


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

    await validate_schema(connection)
    return CURRENT_SCHEMA_VERSION


async def read_schema_version(connection: AsyncConnection) -> int:
    result = await connection.execute(
        text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    )
    return int(result.scalar_one())
