# Cancellation and Runner Reconnect Safety

This document defines the safety rules for stopping remote Codex executions and replacing a Runner WebSocket connection.

## Safety invariants

Remote Control must preserve these invariants:

1. A Job is not terminally `CANCELLED` until the execution process is known to be terminal.
2. A Project Session stays locked while cancellation is pending.
3. Sending `JOB_CANCEL` is not cancellation acknowledgement.
4. A Runner disconnect is not cancellation acknowledgement.
5. An old WebSocket may not mark a replacement connection offline or fail work owned by that replacement connection.

These rules prevent two Codex processes from using the same persistent Project Session and repository concurrently.

## Cancellation lifecycle

For a running Job:

```text
RUNNING
  |
  | user /stop or API cancel
  v
CANCELLING
  |
  | JOB_CANCEL sent to Runner
  | Project Session remains locked
  v
Runner terminates Codex process
  |
  | JOB_RESULT / terminal result
  v
CANCELLED
  |
  +-- Project Session lock released
```

The Controller records `JOB_CANCELLING` through the normal state-transition ledger and records `CANCEL_CONFIRMED` after terminal confirmation.

If the Runner disconnects or does not acknowledge cancellation within the transport timeout, the Job remains `CANCELLING`. The Project Session is intentionally not released.

When an execution id is known, the recovery row is persisted with:

```text
mode=CANCEL
execution_id=<remote execution id>
```

A later Runner reconnect can report that execution id. The Controller adopts the execution only for the purpose of completing the pending cancellation and sends `JOB_CANCEL` again. It releases the Project Session only after a terminal result arrives.

Relevant events:

- `CANCEL_PENDING`
- `CANCEL_CONFIRMED`
- `RUNNER_CANCEL_ADOPTED`
- `CANCEL_RECONCILE_WAIT`
- `CANCEL_RECONCILED_LOCAL`

### Controller restart

A local Codex subprocess cannot survive its owning Controller process. Therefore a local Job found in `CANCELLING` at Controller startup can be finalized as `CANCELLED`.

A remote Job found in `CANCELLING` is different. The remote process may still exist, so the Controller keeps the Job in `CANCELLING` and preserves `mode=CANCEL` until the Runner reports the execution again.

## Runner reconnect replacement

A Runner is identified by `host_id`, but each WebSocket object represents one connection generation.

When a new WebSocket registers the same `host_id`:

1. the new socket becomes the active connection;
2. the previous socket is closed with code 1012;
3. cleanup from the previous socket checks connection identity;
4. if that socket is no longer active, its detach is stale and performs no Host or pending-run cleanup.

```text
old socket ----X
                \
host_id ---------- new socket (active)
```

Only detaching the currently active socket may:

- remove the Host connection;
- resolve pending executions as `retry_kind=host`;
- fail pending Project Operations;
- mark the Host offline.

This prevents a reconnect race where the old socket's `finally` block would otherwise invalidate the newly established connection.

## Operational interpretation

`CANCELLING` means cancellation was requested but process termination is not yet confirmed. Do not start another Job that requires the same Project Session while this state remains.

If a remote Runner cannot reconnect and the old execution cannot be proven dead, keeping the session locked is intentional. Availability is traded for repository/session safety rather than guessing that the old process stopped.
