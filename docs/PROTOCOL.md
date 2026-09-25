# Runner Protocol

Endpoint:

```text
/ws/runner
```

Each message contains:

```text
protocol_version
type
id
timestamp
payload
```

Remote Control 0.11.0 uses **Runner Protocol version 2**. Protocol v1 peers are rejected so process-safety semantics cannot be weakened by a mixed Controller/Runner deployment.

## Connection lifecycle

Runner → Controller:

- `HOST_REGISTER` including `runner_boot_id`
- `RUNNING_JOBS`
- `HEARTBEAT`

Runner execution snapshots include `execution_id`, `session_id`, `working_directory`, and `started_at`. The Controller can use the working-tree lease plus the latest unambiguous snapshot to recover an execution id that was not persisted before a Controller crash.

## Agent execution

Controller → Runner:

- `JOB_START`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_CANCEL`

Runner → Controller:

- `JOB_ACCEPTED`
- `JOB_PROGRESS`
- `JOB_RESULT`
- `JOB_ERROR`
- `SESSION_STARTED`
- `HUMAN_GATE`

Controller → Runner after durable result disposition:

- `JOB_RESULT_ACK`

The Runner keeps a terminal result in its durable execution journal until `JOB_RESULT_ACK` arrives. An unknown `JOB_CANCEL` target returns `JOB_ERROR` with `retry_kind=cancel_unconfirmed`; it is never treated as termination confirmation.

R4 restart adoption continues to use persisted `execution_id` plus `RUNNING_JOBS`.

## R5 Project Operation RPC

Controller → Runner:

```text
PROJECT_OPERATION_REQUEST
  request_id
  project_id
  working_directory
  operation
  payload
```

Runner → Controller:

```text
PROJECT_OPERATION_RESULT
  request_id
  result
```

or:

```text
PROJECT_OPERATION_ERROR
  request_id
  error
```

`request_id` correlates a short deterministic project operation and is separate from a Codex `execution_id`.

Allowed operation names are enforced on the Runner:

- `git_snapshot`
- `project_os_status`
- `project_os_next`
- `project_os_context`
- `project_os_claim`
- `project_os_submit`

The protocol does not expose a generic command/shell message.

If the Runner disconnects, pending Project Operations fail with a recoverable connection error at the Controller boundary.

## Human Gate

`HUMAN_GATE` remains the structured approval event introduced in R3.

## Reserved

`JOB_PAUSE` remains reserved as a dedicated transport primitive.


## Protocol v2 safety boundary

Protocol v2 is intentionally not wire-compatible with v1. Controller and Runner must be upgraded together. See [Process Safety P0 Closure](PROCESS_SAFETY_P0.md).
