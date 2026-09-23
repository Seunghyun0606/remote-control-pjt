# Project Adapters

R5 introduces a boundary between Remote Control runtime orchestration and project-specific state.

## Adapter contract

Conceptually:

```text
ProjectAdapter
  prepare(...)
  submit_result(...)
```

The Controller selects an adapter from the registered Project definition. Codex does not choose the adapter.

## Generic Git

`generic_git` keeps Remote Control usable without Project OS.

Prepare phase:

```text
git branch --show-current
git status --short --branch
git diff --stat
```

These commands are executed with direct subprocess argv. No shell string is constructed.

There is no adapter-level result submission step.

## Project OS

`project_os` uses the installed `projectctl` CLI as the only canonical-state mutation boundary.

Prepare phase:

```text
projectctl status --json
projectctl next --role <role> --json
projectctl context <task> --role <role>
projectctl claim <task> --role <role>
```

After a successful Codex turn:

```text
projectctl submit <task> <temporary-result.yaml> --role <role> --actor <actor>
```

Remote Control does not write:

- `.project-os/state/backlog.yaml`
- `.project-os/state/current.yaml`
- Project OS handoff directories directly

The temporary result file is generated outside the canonical Project OS tree and passed to `projectctl submit`.

## Runtime ProjectWork

Remote Control stores only the execution binding:

```text
job_id
adapter
host_id
task_id
role
status
result_path
next_task_id
error
timestamps
```

This is runtime metadata, not a copy of the Project OS plan.

Typical statuses:

```text
SELECTED
CLAIMED
PREPARED
SUBMIT_FAILED
SUBMITTED
```

## Host pinning

Before a Project OS task is claimed, normal Host routing applies.

After Task selection/claim, ProjectWork records the execution Host. Recovery reuses that Host even when the original Job requested `auto`.

Reason:

```text
same logical Project OS task
must not silently continue
against a different unsynchronized checkout
```

If adapter preparation somehow receives a different Host for an existing binding, it fails rather than crossing the boundary.

## Restart reconciliation

### Claim boundary

A crash may occur after `projectctl claim` updates Project OS but before Remote Control persists `CLAIMED`.

On retry the adapter reads `projectctl status --json`. If the persisted Task already appears in `current_tasks`, Remote Control records the claim as reconciled and does not issue a duplicate claim.

### Submit boundary

A successful Codex turn moves the Job to `WAITING_AGENT` while the adapter submits the implementation handoff.

If the Controller restarts in that state:

```text
WAITING_AGENT
  ↓
WAITING_HOST + RecoveryMode.FINALIZE
  ↓ original Host available
STARTING
  ↓
projectctl submit only
  ↓
COMPLETED
```

Codex is not rerun just to repeat the handoff.

`projectctl submit` itself is used as the canonical idempotency boundary.

## Completion semantics

A Remote Control Job becomes `COMPLETED` after the implementation turn succeeds and the implementation handoff is submitted.

This does **not** mean the Project OS task is approved or done. Independent Project OS evaluation still decides PASS / REWORK / HUMAN_GATE.

## Remote Host operations

The Controller never sends arbitrary shell text to the Desktop Runner.

Allowed Project Operation RPC values are:

- `git_snapshot`
- `project_os_status`
- `project_os_next`
- `project_os_context`
- `project_os_claim`
- `project_os_submit`

Runner-side code maps each operation to fixed argv construction.

## Requirements

Every Host that may execute a `project_os` project needs:

- the project checkout
- matching `.project-os` scaffold/state
- `projectctl` installed
- Git
- Codex CLI and local Codex authentication

Remote Control does not copy Project OS state or Codex credentials between Hosts.
