# Recovery and Scheduler

R4 turns recoverable runtime failures into explicit persisted states instead of terminal failures.

## Recovery Registry

Recovery metadata is stored in a separate SQLite table so existing R0-R3 `jobs` tables do not require an in-place ALTER migration.

A Recovery record contains:

```text
job_id
kind
mode
attempt_count
next_retry_at
execution_id
resume_instruction
last_error
created_at
updated_at
```

Kinds:

- `HOST`
- `QUOTA`
- `RESTART`

Modes:

- `START`: start the Job when a Host becomes available
- `RESUME`: continue from Codex session/repository state
- `ADOPT`: runtime metadata for a remote execution that may still be alive
- `CANCEL`: preserve a remote cancellation intent until execution termination is confirmed

Recovery records are removed when the Job reaches a terminal state.

A remote Job in `CANCELLING` is deliberately non-terminal. If its Runner disconnects, the recovery row keeps `mode=CANCEL` and the execution id so a reconnect can re-adopt that execution and finish cancellation safely. The Project Session is not unlocked merely because the cancel request was sent.

## WAITING_HOST

A valid but offline explicit Host does not cause immediate Job failure.

```text
/run demo --host desktop-main
           ↓ desktop offline
      WAITING_HOST
           ↓ heartbeat/reconnect
        ASSIGNED
           ↓
        RUNNING
```

For `auto`, HostRouter reevaluates the configured allowed Hosts. A compatible fallback may be selected.

Heartbeat expiry is persisted as `OFFLINE`; it is not only a presentation-time calculation.

## WAITING_QUOTA

Codex quota detection accepts:

1. structured rate/usage-limit events
2. structured retry/reset fields when present
3. known quota/rate-limit text as a fallback
4. Codex error codes such as `usage_limit_exceeded` / `rate_limit_exceeded`
5. text-only reset hints such as `try again at Sep 25th, 2026 2:20 PM`

Structured reset timestamps have priority. For remote execution, timezone-less quota hints are interpreted on the Runner where Codex produced the message and normalized to an explicit UTC timestamp before they reach the Controller. This prevents a UTC Controller and a local-time Desktop Runner from assigning different meanings to the same reset hint.

If Codex provides a reset timestamp, Remote Control schedules the retry for **reset time + grace period**. The default grace is 600 seconds (10 minutes), so a Codex reset at 17:37 is retried at 17:47 rather than exactly at the boundary. A time-only hint that is already in the past is treated as stale and does **not** roll over to the next day; the normal exponential backoff is used instead.

Otherwise the default policy is:

```text
attempt 1: 30 minutes
attempt 2: 60 minutes
attempt 3+: 120 minutes
```

Configure:

```dotenv
REMOTE_CONTROL_QUOTA_RETRY_INITIAL_SECONDS=1800
REMOTE_CONTROL_QUOTA_RETRY_MAX_SECONDS=7200
REMOTE_CONTROL_QUOTA_RESET_GRACE_SECONDS=600
```

The attempt count survives retries and resets only when the Job completes/cancels/fails.

Messenger observability:

```text
/status
JOB-... demo WAITING_QUOTA host=desktop-main recovery=QUOTA attempt=2 retry_at=...

/job JOB-...
Recovery: QUOTA
Recovery mode: RESUME
Retry attempt: 2
Next retry: ...
```

When the retry becomes due, the Scheduler sends a quota-resumed notification and prefers the existing Codex session.

A new Codex thread is **not** a generic recovery for arbitrary resume failure. Fallback is limited to an explicitly missing saved session/thread/rollout or `SESSION_IDENTITY_MISMATCH`. Syntax, executable, permission, filesystem and unknown resume failures remain Job failures so the root cause is not hidden.

## Recovery Scheduler

Default interval:

```dotenv
REMOTE_CONTROL_SCHEDULER_INTERVAL_SECONDS=15
```

Each tick performs:

1. stale heartbeat expiry
2. Human Gate expiry
3. due Host recovery
4. due quota recovery

## Controller restart

Before normal Job recovery, startup reconciles Project Session locks. A lock is released only when its owning Job is missing or already terminal. Locks for active recovery/cancellation states are preserved. Stale releases are recorded as `PROJECT_SESSION_STALE_LOCK_RELEASED`.

At startup the Controller scans persisted jobs in active execution states.

Those jobs are converted to `WAITING_HOST` while recovery is reconciled.

### Local execution

A local subprocess cannot survive its owning Controller process. If a Codex session id exists, the Controller resumes that session. Otherwise it starts from repository state.

### Remote execution

The Desktop Runner is a separate process, so its Codex child may continue while the Controller is unavailable.

The Runner retains in-memory:

- active execution id
- current session id
- completed results that could not be delivered

After reconnect:

```text
HOST_REGISTER
RUNNING_JOBS
  - running_jobs
  - completed_jobs
```

The Controller matches `execution_id` and adopts the existing execution. A later `JOB_RESULT` resolves the adopted handle normally.

Default remote restart grace:

```dotenv
REMOTE_CONTROL_RESTART_GRACE_SECONDS=10
```

If the Runner does not report the old execution before the grace expires, the Scheduler resumes through Codex session/repository state.

The Runner's reconnect buffer is process memory. If the Runner process itself dies, its active child execution cannot be adopted through this mechanism.

## Approval expiry

R4 activates the existing `expires_at` field.

When a pending Approval expires:

```text
PENDING → EXPIRED
WAITING_HUMAN Job → FAILED
```

The failure is explicit so a destructive or architecture-changing decision is never guessed automatically after its approval window closes.

## Runtime API

```text
GET /recovery
```

returns current non-terminal recovery metadata for diagnostics.

## Event Ledger

Important R4 events include:

- `HOST_OFFLINE`
- `HOST_WAIT`
- `HOST_WAIT_RESUMED`
- `QUOTA_WAIT`
- `QUOTA_RESUMED`
- `CONTROLLER_RECONCILE_WAIT`
- `RUNNER_JOB_ADOPTED`
- `HUMAN_GATE_EXPIRED`


## FAILED Job retry

`FAILED` is terminal and is not automatically retried.

Use:

```text
/retry JOB-...
```

for a failed `generic_git` Job after fixing the underlying environment/problem.

Remote Control preserves the original FAILED Job and creates a new Job. If the original Job already has a Codex external session ID, the retry Job attempts to resume that session; otherwise it starts a fresh Codex turn using the original instruction.

Project OS Jobs are intentionally excluded from direct retry cloning. Use `/run` so the adapter can re-read canonical Project OS task state.

`WAITING_HOST` and `WAITING_QUOTA` remain automatic recovery states. Their recovery `last_error` is included in `/status` and `/job` diagnostics.


## Working-tree execution lease

Recovery keeps a DB-backed execution lease for every non-terminal Job that has an assigned host. The lease key is derived from `host_id + canonical working directory`, not from messaging user identity.

This means WAITING_HOST, WAITING_QUOTA, WAITING_HUMAN, PAUSED and CANCELLING Jobs continue to reserve their checkout. Another Telegram/Slack/API identity cannot start a concurrent writer on the same host/path.

On startup stale leases for missing/terminal Jobs are removed and leases for non-terminal Jobs are reconstructed. If persisted active Jobs conflict on the same working tree, Controller startup fails instead of choosing one implicitly.

For local jobs, startup also verifies and terminates a persisted Codex process tree before normal recovery. See [Process Safety P0 Closure](PROCESS_SAFETY_P0.md).
