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

Schema migration tracking remains versioned and the current schema version is 2. Existing installations are brought under version tracking through idempotent migrations.

Future schema changes must add a numbered migration to `storage/migrations.py` rather than depending on SQLAlchemy `create_all()` to alter existing tables.

A version number alone is no longer considered sufficient proof of schema compatibility. After migrations, every startup validates mapped tables, columns, primary keys, type signatures, nullable flags, indexes, and unique constraints. Missing or unexpected objects are treated as schema drift and startup fails closed.

A database whose recorded schema version is newer than the running binary is also rejected instead of being opened with an older schema contract.

## P2-5 — dependency constraints

Direct dependency intent remains documented in `constraints/runtime.txt` and `constraints/dev.txt`, while the complete transitive CI snapshot is pinned in:

```text
constraints/lock.txt
```

Linux Python 3.11/3.12 and Windows smoke all install with:

```bash
pip install -c constraints/lock.txt -e ".[dev]"
```

The lock contains exact pins for every resolved application direct and transitive package. Platform-only dependencies such as `colorama` are constrained but are installed only when selected by the platform resolver. Tests also verify that every declared direct dependency is present in the lock.

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

1. restart the Controller so schema migrations and structural drift validation run;
2. restart Remote Runners so chunk-framed Codex stdout handling is active;
3. no manual DB migration command is required for this baseline;
4. use the constraint files for reproducible installs where desired;
5. existing active Project Session locks are preserved unless they are provably stale.


## P0 process-safety follow-up

A later end-to-end lifecycle review identified process-restart and cross-channel working-tree concurrency gaps not covered by P2. They are closed in Remote Control 0.11.0; see [Process Safety P0 Closure](PROCESS_SAFETY_P0.md).


## P2 security and integrity follow-up — 0.11.2

The final review left three P2 gaps after the earlier runtime-hardening work.

### Server-owned Control API identity

`RunRequest.requested_by` and `ApprovalResponseRequest.user_id` are removed. Those extra fields are rejected with validation errors.

API ownership is derived only from:

```dotenv
CONTROLLER_API_PRINCIPAL=api:controller
```

This principal must use the `api:` namespace and contain no whitespace.

### Structural schema drift validation

The migration version is checked first, then the live schema is compared with SQLAlchemy metadata. A database can no longer claim the expected migration version while silently missing an index, carrying an unexpected column, or differing in key/nullability/type structure.

### Full dependency lock

`constraints/lock.txt` is the reproducible CI/deployment constraint set. The older runtime/dev files remain useful as direct-dependency intent, not as a complete lock.
