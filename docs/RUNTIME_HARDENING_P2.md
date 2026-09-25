# Runtime Hardening — P2

P2 completes the lower-priority runtime integrity backlog left after the P0/P1 review.

## P1 residual closure

One residual ownership issue was found in Telegram multi-Job selection callbacks.

Previously the pending selection token was removed before checking which authorized user owned it. Another allowed user could therefore consume the token even though the subsequent ownership check rejected the action.

The flow is now:

```text
lookup token
→ verify owner
→ verify TTL
→ consume token
→ steer selected Job
```

Selection tokens also expire after `REMOTE_CONTROL_TELEGRAM_SELECTION_TTL_SECONDS` (default 300 seconds).

## P2-1 — Codex JSONL chunk framing

Codex stdout is no longer consumed with line iteration backed by a large asyncio StreamReader separator limit.

The Runner now:

1. reads stdout in 64 KiB chunks;
2. performs newline framing itself;
3. retains at most 32 MiB for one JSONL record;
4. if one record exceeds that bound, discards the overflow until the newline;
5. emits a bounded `raw_output_truncated` event instead of failing the Job.

This removes the `Separator is not found, and chunk exceed the limit` failure mode while keeping memory usage bounded.

## P2-2 — stale Project Session lock reconciliation

Controller startup scans locked Project Sessions before normal Job recovery.

A lock is released only when its `locked_by_job_id`:

- no longer exists, or
- points to a terminal Job (`FAILED`, `CANCELLED`, `COMPLETED`).

Locks owned by active/non-terminal Jobs remain untouched, including `CANCELLING`, `WAITING_HOST`, `WAITING_QUOTA`, and `WAITING_HUMAN`.

Releases are recorded as:

```text
PROJECT_SESSION_STALE_LOCK_RELEASED
```

## P2-3 — unified event payload sanitization

Local Controller execution and Remote Runner execution now use the same sanitizer.

The shared sanitizer:

- truncates large text/message/error fields;
- truncates command and nested item output;
- limits option lists;
- truncates option labels/values;
- preserves session identity and quota reset metadata;
- normalizes timezone-less quota reset hints at the point where they are interpreted.

This keeps SQLite/event transport growth predictable and removes local/remote observability differences.

## P2-4 — versioned database migration runner

Database initialization no longer directly relies on `Base.metadata.create_all()` as the schema-management contract.

Remote Control now maintains:

```text
schema_migrations(version, applied_at)
```

Schema version 1 is the current baseline. Existing installations are safely brought under version tracking because baseline table creation is idempotent.

Future schema changes must add a numbered migration to `storage/migrations.py` rather than depending on SQLAlchemy `create_all()` to alter existing tables.

A database whose recorded schema version is newer than the running binary is rejected instead of being opened with an older schema contract.

## P2-5 — dependency constraints

Direct runtime and development dependencies used by CI are pinned in:

```text
constraints/runtime.txt
constraints/dev.txt
```

CI installs with:

```bash
pip install -c constraints/dev.txt -e ".[dev]"
```

The project metadata keeps compatible version ranges for packaging, while CI/deployment can opt into repeatable direct dependency versions through constraints.

## P2-6 — broader Windows CI

Windows CI now covers more than executable resolution and command parsing.

It includes:

- Codex Runner framing/resume behavior;
- RunnerGateway transport;
- Project Session lifecycle;
- runtime hardening/SQLite behavior;
- settings and command routing.

Linux Python 3.11/3.12 still runs the complete test suite.

## P2-7 — Telegram pending selection TTL

Pending multi-Job steering choices are in-memory and bounded by time.

Default:

```dotenv
REMOTE_CONTROL_TELEGRAM_SELECTION_TTL_SECONDS=300
```

Expired tokens are purged when new choices are created and rejected when selected after expiry.

## Upgrade notes

After pulling this change:

1. restart the Controller so schema migration v1 is recorded;
2. restart Remote Runners so chunk-framed Codex stdout handling is active;
3. no manual DB migration command is required for this baseline;
4. use the constraint files for reproducible installs where desired;
5. existing active Project Session locks are preserved unless they are provably stale.
