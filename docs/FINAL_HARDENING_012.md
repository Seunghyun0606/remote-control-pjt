# Final Generation & Ownership Hardening — 0.12.0

Remote Control 0.12.0 closes the remaining controller/runner generation and crash-consistency gaps found after the v0.11 process-safety work.

## Safety invariants

1. Only one Controller process may own a runtime database at a time, and only one Runner process may own a Runner journal at a time.
2. A remote `host_id` is not sufficient identity. A Runner has a stable `runner_instance_id` and a per-process `runner_boot_id`.
3. A different Runner instance cannot take over a host while that host owns active Jobs, working-tree leases, or unacknowledged executions.
4. Every new remote execution is assigned an `execution_id` and persisted to the Controller ownership ledger before it is sent to the Runner.
5. A remote result is ACKed only after the Controller has durably associated it with its owning Job.
6. Job assignment and working-tree lease acquisition commit in one database transaction.
7. Authoritative Job lifecycle mutations and `JOB_*` lifecycle events are atomic as of 0.12.1; supplemental audit telemetry remains best-effort.
8. Runner terminal-result journal failures are safety-fatal and stop automatic reconnect/new work.

## 1. Controller singleton

The Controller acquires an OS-level nonblocking file lock before database initialization or recovery reconciliation.

For SQLite the default lock is beside the database:

```text
remote-control.db
remote-control.db.controller.lock
```

An optional override is available:

```dotenv
REMOTE_CONTROL_CONTROLLER_LOCK_PATH=/absolute/path/controller.lock
```

A second Controller targeting the same runtime fails before it can inspect or terminate persisted Codex PIDs.

The lock is advisory but process-scoped:
- POSIX uses `flock(LOCK_EX | LOCK_NB)`.
- Windows uses `msvcrt.locking(... LK_NBLCK ...)`.

The file may remain after shutdown; ownership is the OS lock, not file existence.

## 2. Stable Runner identity

Runner Protocol v3 separates:

```text
host_id
  logical configured execution host

runner_instance_id
  stable identity persisted in the Runner journal

runner_boot_id
  new value on every Runner process start
```

The journal contains the stable instance id together with execution records. Reopening the same journal preserves it.

A reconnect from the same process uses the same instance id and boot id and may replace its stale WebSocket generation.

A different boot id is never allowed to replace a still-live WebSocket generation. The Runner also holds an OS-level singleton lock beside its journal before startup recovery, so a second process sharing the same journal cannot inspect/terminate the first Runner's active Codex processes.

A different instance using the same `host_id` is rejected while any of these exist:
- a non-terminal Job assigned to the host;
- a working-tree execution lease on the host;
- an unacknowledged remote execution ownership row.

This prevents an unrelated Runner or a Runner using a different journal from claiming that the previous process is absent.

## 3. Durable remote execution ownership

Schema v4 adds `remote_executions`.

Each remote turn records:

```text
execution_id
job_id
host_id
runner_instance_id
working_directory
session_id
state
started_at
result_received_at
acknowledged_at
```

For new work the Controller performs:

```text
reserve execution_id
→ persist ACTIVE ownership row
→ send JOB_START/JOB_RESUME/JOB_STEER
→ receive terminal result
→ persist RESULT_RECEIVED
→ persist Job result/state
→ send JOB_RESULT_ACK
→ mark ownership ACKNOWLEDGED
```

After Controller restart, a completed result is matched by exact ownership rather than only by working directory. Terminal Jobs with retransmitted completed results are ACKed directly.

The legacy recovery fallback is deliberately narrow. An unowned execution is considered only when its session id matches the Job's persisted Codex session, its working directory matches, and its Runner start time is not older than the Job.

## 4. Atomic assignment and working-tree lease

Previously:

```text
lease COMMIT
→ Job ASSIGNED COMMIT
```

left a crash window where a `QUEUED` Job could permanently retain a lease.

Now the lease row and:

```text
Job.state = ASSIGNED
Job.assigned_host = <host>
```

are committed in one database transaction.

Startup also repairs legacy states:
- lease whose Job is missing or terminal → release;
- lease whose Job has no `assigned_host` → release;
- active Job/lease host or working-directory mismatch → fail closed;
- persisted `QUEUED` Job → `WAITING_HOST` with recovery metadata.

`JOB_ASSIGNED` is committed in the same transaction as the Job assignment and execution lease as of 0.12.1. Other authoritative `JOB_*` lifecycle events are likewise committed atomically with their Job mutation. Supplemental execution-lease/diagnostic audit events remain best-effort telemetry, so their failure cannot strand a successfully committed lease or prevent terminal resource cleanup. See [Lifecycle Atomicity P1](LIFECYCLE_ATOMICITY_P1.md).

## Runner terminal journal failure

A Runner must durably persist a terminal result before removing the execution from its in-memory running set.

If `journal.complete()` fails:

```text
do not forget running execution
→ RunnerSafetyError
→ close Controller connection
→ refuse automatic reconnect/new work
```

Similarly, failure to durably remove a `JOB_RESULT_ACK` entry is safety-fatal.

## Compatibility

Remote Control 0.12.0 uses:

```text
Package version: 0.12.0
Runner Protocol: v3
Database schema: v4
```

Protocol v1/v2 peers are rejected. Controller and all Remote Runners must be upgraded together.

## Upgrade

1. Stop the old Controller and all Remote Runners cleanly when possible.
2. Back up the SQLite database and Runner journal files.
3. Pull the same 0.12.0 build on Controller and Runner hosts.
4. Install using `constraints/lock.txt`.
5. Start exactly one Controller for the runtime.
6. The Controller automatically migrates schema v2 → v4.
7. Start each Remote Runner with its existing journal path.
8. Verify the host reconnects with the same stable Runner instance identity.
9. Run a short remote Job and verify completion/ACK.

Do not delete a Runner journal to bypass an identity or process-safety refusal. Changing/deleting the journal intentionally creates a new Runner instance and may be rejected while prior host-owned work is unresolved.
