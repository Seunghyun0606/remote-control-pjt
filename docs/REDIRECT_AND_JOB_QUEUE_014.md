# Redirect & Durable Job Queue — 0.14.0

Remote Control 0.14.0 focuses on the core remote-work experience: changing a running Codex direction immediately and submitting more work without turning checkout contention into a failed Job.

## Immediate redirect

```text
RUNNING turn A
   ↓ /redirect <new instruction>
PAUSED
   ↓ current handle/process termination confirmed
RUNNING
   ↓ same Codex session resume
turn B with replacement instruction
```

`/redirect` reuses the existing safe pause/resume lifecycle rather than inventing a second process-control path. The current turn must be `RUNNING`; Human Gate, quota, host-wait, queued and terminal Jobs are not silently overridden.

`/steer` remains deliberately different:

- `/steer`: do not interrupt; apply after the current turn.
- `/redirect`: stop the current turn and apply now in the same session.

## Working-tree Job queue

When a new explicit Job targets a working tree already owned by another Job, it enters:

```text
WAITING_LEASE
```

instead of `FAILED`.

The waiting Job persists:

- original instruction;
- requested/selected host;
- Project/Codex session hint;
- durable `LEASE` recovery record.

It does not own:

- the working-tree execution lease;
- the Project Session lock;
- a Codex process.

When the previous Job terminates, the recovery scheduler retries the oldest due queue item. After the lease is acquired, Remote Control reacquires the current Project Session and refreshes the Job's external Codex session before starting it.

This gives the normal flow:

```text
Job A RUNNING

Job B WAITING_LEASE
Job C WAITING_LEASE

A COMPLETED
  → B RUNNING
  → C remains WAITING_LEASE

B COMPLETED
  → C RUNNING
```

Different checkouts can still start independently in the same scheduler pass.

## Restart behavior

`WAITING_LEASE` is durable. Controller startup rearms missing/stale queue retry timestamps. A queued Job must not own a lease; if startup finds such an orphan lease it releases it before recovery continues.

## Manual retry

```text
/retry <waiting-lease-job-id>
```

forces an immediate lease attempt. If the checkout or Project Session is still busy, the Job remains queued.

## Compatibility

- Package: 0.14.0
- Runner Protocol: v3
- Database schema: v5

No protocol or schema migration is required from 0.13.1.
