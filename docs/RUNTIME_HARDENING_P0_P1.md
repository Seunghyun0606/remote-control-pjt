# Runtime Hardening — P0/P1 Closure

This document records the runtime-safety review and the P0/P1 fixes applied after the September 2026 remote execution incidents.

## Status

### P0 — closed

The P0 review covered the two paths that could allow repository/session overlap:

1. remote cancellation before process termination was confirmed;
2. stale Runner WebSocket cleanup invalidating a replacement connection.

Both are closed by the acknowledgement-based `CANCELLING` lifecycle and connection-generation-safe Runner detach behavior documented in [Cancellation and Runner Reconnect Safety](CANCELLATION_AND_RECONNECT.md).

A follow-up review after those changes found no remaining P0 item in the reviewed Job/Runner/session lifecycle.

## P1-1 — conservative Codex session fallback

A failed `codex exec resume` no longer automatically starts a new Codex thread.

Fallback is allowed only when the failure explicitly means that the previous session cannot safely be resumed:

- saved session/thread is not found;
- rollout/session storage is explicitly not found;
- `SESSION_IDENTITY_MISMATCH` is detected.

Examples that do **not** allow fallback:

- CLI syntax/option errors;
- executable launch failures;
- filesystem or permission errors;
- authentication failures;
- unknown Codex/internal errors.

Unsafe or unknown resume failures remain failures so the original cause is preserved. This prevents duplicate work, hidden environment faults and unnecessary token usage.

The `SESSION_RESUME_FAILED` event now includes:

```text
fallback_allowed=true|false
```

Only `fallback_allowed=true` can lead to `SESSION_FALLBACK_NEW`.

## P1-2 — RunnerDaemon supervision

The remote Runner is expected to be unattended and long-lived.

The connection supervisor now:

- re-raises only explicit daemon cancellation;
- reconnects after expected socket/network failures;
- logs and reconnects after unexpected protocol/handler exceptions instead of terminating the Runner process;
- supervises heartbeat and receive loops together, so failure of either tears down that connection and starts reconnect;
- awaits cancelled heartbeat/receiver tasks so no unobserved task exception remains;
- tracks background execution-finalizer tasks and consumes/logs unexpected task exceptions.

An unexpected envelope/handler failure can therefore disrupt one connection generation but should not permanently stop the Runner daemon.

## P1-3 — SQLite and event-write resilience

SQLite remains the runtime persistence store, but concurrent Controller activity can create short write contention.

For SQLite connections Remote Control now configures:

```text
PRAGMA busy_timeout=5000
PRAGMA journal_mode=WAL
PRAGMA synchronous=NORMAL
```

Event writes retry transient SQLite `database is locked` / `database is busy` errors twice with short backoff.

Other database errors are not swallowed.

Raw `AGENT_EVENT` persistence is telemetry. If that write still fails after repository handling, the failure is logged but the active Codex execution continues so observability failure does not become execution failure.

State-changing persistence such as Job, Session, Approval and Recovery updates remains fail-fast.

## P1-4 — Control API authentication

The Controller still binds to loopback by default.

```dotenv
REMOTE_CONTROL_API_HOST=127.0.0.1
```

When binding to a non-loopback address such as `0.0.0.0`, `CONTROLLER_API_TOKEN` is required at startup.

```dotenv
REMOTE_CONTROL_API_HOST=0.0.0.0
CONTROLLER_API_TOKEN=<random-secret>
```

When `CONTROLLER_API_TOKEN` is configured, HTTP endpoints except `/health` require one of:

```http
Authorization: Bearer <token>
```

or:

```http
X-Remote-Control-Token: <token>
```

The Runner WebSocket remains separately authenticated by `CONTROLLER_RUNNER_TOKEN`.

The Web UI uses the same HTTP boundary. If API authentication is enabled, requests to `/ui` and `/dashboard` also require the API token. For browser-facing remote access, prefer an authenticated reverse proxy/private network rather than exposing the Controller directly.

## Operational checks after upgrade

After pulling the version containing these changes:

1. restart the Controller;
2. restart every Remote Runner using the same deployment;
3. if `REMOTE_CONTROL_API_HOST` is not loopback, set `CONTROLLER_API_TOKEN` before restart;
4. verify `remote-control doctor`;
5. verify `/health`;
6. verify authenticated API access if the HTTP API is used;
7. verify a Remote Runner reconnects after a forced WebSocket disconnect;
8. verify an invalid resume error remains `FAILED` rather than creating an unrelated new thread.

## P2 follow-up

The lower-priority backlog from this review has been implemented in [Runtime Hardening — P2](RUNTIME_HARDENING_P2.md), including chunk-framed Codex JSONL, stale Project Session lock reconciliation, shared event sanitization, versioned DB migrations, dependency constraints, broader Windows CI, and Telegram selection TTL/ownership.
