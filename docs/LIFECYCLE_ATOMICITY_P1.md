# Lifecycle Atomicity P1 — 0.12.1

Remote Control 0.12.1 closes the remaining P1/P2 crash-consistency gap between authoritative Job lifecycle mutations and their lifecycle events.

## Problem

Before 0.12.1, Job state and lifecycle events were committed separately.

For example:

```text
Job.state = COMPLETED COMMIT
→ JOB_COMPLETED event COMMIT
```

A crash or event-write failure between those operations could leave the database with a changed Job state but without the corresponding lifecycle event.

0.12.0 reduced the operational impact by treating some audit-event failures as best-effort so terminal cleanup would still run. That avoided cleanup being blocked, but it did not make the authoritative lifecycle history atomic.

## 0.12.1 invariant

The following authoritative lifecycle mutations now commit the Job row and Event row in the same database transaction:

- `JOB_CREATED`
- `JOB_RETRY_CREATED`
- every `JOB_<STATE>` transition
- `JOB_ASSIGNED` together with the working-tree execution lease assignment

The invariant is:

```text
authoritative Job mutation
+ authoritative lifecycle Event
(+ execution lease for ASSIGNED)
= one DB transaction
```

If the lifecycle event cannot be inserted, the Job mutation is rolled back.

This prevents:

```text
state changed
event missing
```

and for assignment prevents:

```text
lease exists
Job ASSIGNED
JOB_ASSIGNED event missing
```

## Telemetry boundary

Not every Event row is authoritative lifecycle history.

Operational and diagnostic events such as execution-lease acquired/released telemetry remain best-effort after the authoritative transaction. Their failure is logged but does not roll back a valid Job lifecycle mutation.

This separation is intentional:

```text
Authoritative:
  Job lifecycle state + JOB_* lifecycle event
  => atomic

Telemetry:
  diagnostics / supplemental lease audit
  => best effort
```

## Concurrency guard

Lifecycle transitions also verify the persisted current state inside the same transaction.

If another path changed the Job after the caller read it, the repository refuses the stale transition instead of committing an event for the wrong source state.

## Regression coverage

0.12.1 adds tests proving:

1. a lifecycle transition writes the state and event together;
2. event insertion failure rolls back the Job transition;
3. event insertion failure rolls back initial Job creation;
4. atomic lease assignment still succeeds when supplemental audit telemetry fails;
5. `JOB_ASSIGNED` is present even when the supplemental EventRepository path is unavailable.

The tests run in the normal Linux Python 3.11/3.12 suite and in Windows smoke via `tests/test_final_hardening.py`.

## Compatibility

- Package: 0.12.1
- Runner Protocol: v3
- Database schema: v4
- No migration is required from 0.12.0 to 0.12.1.
