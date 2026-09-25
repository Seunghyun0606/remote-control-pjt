# Process Safety P0 Closure

This document defines the process-lifecycle safety rules introduced in Remote Control 0.11.0 / Runner Protocol v2.

## Safety invariants

Remote Control now treats these as hard safety invariants:

1. A Job must not release its working-tree lease while its Codex process may still be alive.
2. A persisted PID is never killed only because the numeric PID matches; the process command must also match the expected `--cd <working-directory>`.
3. One physical working tree on one execution host may have only one non-terminal Job lease, regardless of Telegram, Slack, API, user id, or Project Session owner.
4. A remote terminal result remains durable on the Runner until the Controller has durably processed the Job result and sends `JOB_RESULT_ACK`.
5. If process identity or spawn state cannot be proven after a crash, startup fails closed rather than guessing that execution stopped.
6. Controller and Runner must use Runner Protocol v2 together. Protocol v1 is rejected.

## Working-tree execution lease

Project Session ownership and repository execution ownership are separate concepts.

Project Session:

```text
(project_id, owner_user_id)
→ Codex conversational context
```

Execution lease:

```text
(host_id, canonical working_directory)
→ exclusive write execution
```

The lease is stored in the Controller database and remains held through non-terminal states such as:

- `RUNNING`
- `WAITING_HOST`
- `WAITING_QUOTA`
- `WAITING_HUMAN`
- `PAUSED`
- `CANCELLING`

It is released only when the Job reaches a terminal state.

This prevents a Telegram user, Slack user, API caller, or another configured project from starting a second write-capable Codex execution against the same checkout.

Database schema version 2 adds the `execution_leases` table.

## Local Controller process lifecycle

Codex is launched in its own process group/session.

Graceful Controller shutdown:

```text
RUNNING local Job
→ persist WAITING_HOST recovery state
→ terminate Codex process tree
→ confirm termination
→ clear persisted PID
→ keep working-tree lease
→ stop Controller
```

On the next Controller startup, the Job may safely resume from the existing repository/session state.

After an unclean Controller crash, a non-terminal local Job with a persisted PID is checked before recovery. Remote Control verifies the live process command contains the expected working-directory argument and terminates the entire process tree before allowing recovery.

If a Job is persisted as `STARTING`, `RUNNING`, or `CANCELLING` but no PID was durably recorded, the Controller refuses startup because it cannot prove that a Codex process was not created.

## Remote Runner execution journal

Each Runner uses a durable local journal.

Default path:

```text
~/.remote-control/<host-id>-executions.json
```

Override:

```dotenv
REMOTE_RUNNER_STATE_PATH=<path>
```

Execution lifecycle:

```text
STARTING
  reserve journal entry before process spawn
    ↓
RUNNING
  attach and persist PID
    ↓
COMPLETED
  persist terminal AgentRunResult
    ↓
JOB_RESULT
    ↓
Controller durably processes Job state
    ↓
JOB_RESULT_ACK
    ↓
journal entry removed
```

### Runner restart

At Runner startup:

- `COMPLETED` entries are retained and retransmitted.
- `RUNNING` entries are verified against their command line and working directory, then the old process tree is terminated before reconnect.
- `STARTING` entries cause fail-closed startup because the Runner may have crashed between process spawn and durable PID attachment.
- a live PID whose identity cannot be proven also causes fail-closed startup.

A restarted Runner never silently assumes that an old execution is gone.

## Controller crash before execution id persistence

Runner snapshots include:

- `execution_id`
- `session_id`
- `working_directory`
- `started_at`

If the Controller crashed after a remote process began but before it persisted the execution id, the Controller can rebind the Job using its exclusive working-tree lease. If multiple historical durable results exist for that working tree, the newest unambiguous `started_at` is selected. An ambiguous tie is not adopted.

The rebind is recorded as:

```text
RUNNER_EXECUTION_REBOUND
```

## Cancellation

An unknown `JOB_CANCEL` target is not cancellation confirmation.

The Runner returns an error with:

```text
retry_kind=cancel_unconfirmed
```

The Controller keeps the Job non-terminal and retains the working-tree and Project Session locks.

## Result acknowledgement

`JOB_RESULT_ACK` is deliberately delayed.

The Controller first persists the outcome, for example:

- `COMPLETED`
- `FAILED`
- `WAITING_AGENT`
- `WAITING_QUOTA`
- `WAITING_HUMAN`
- confirmed `CANCELLED`

Only after the durable disposition is recorded does the Controller acknowledge the Runner result.

If the Controller dies before the ACK, the Runner keeps the result in its journal and retransmits it on reconnect.

## Protocol compatibility

These semantics require Runner Protocol v2.

A v1 Runner and v2 Controller, or a v2 Runner and v1 Controller, are intentionally incompatible. Upgrade the Controller and all Remote Runners together.

## Upgrade

1. Pull the same 0.11.0 code on Controller and Runner hosts.
2. Install/update dependencies.
3. Stop old Controller and Runner processes.
4. Start the Controller; schema migration v2 is automatic.
5. Start each Runner using the same v2 build.
6. Verify host status and perform a short remote Job.
7. Verify the Runner state journal path is writable.

Do not manually delete a Runner journal merely to bypass a fail-closed startup. A `STARTING` entry or unverifiable live PID means process state is uncertain; inspect the host/process first.
