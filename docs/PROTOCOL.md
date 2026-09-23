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

R5 remains additive on protocol version 1.

## Connection lifecycle

Runner → Controller:

- `HOST_REGISTER`
- `RUNNING_JOBS`
- `HEARTBEAT`

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
