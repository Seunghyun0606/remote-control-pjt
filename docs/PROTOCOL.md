# Runner Protocol

Remote Runner connections are outbound WebSockets from the execution Host to the Controller.

Endpoint:

```text
/ws/runner
```

Each message uses:

```text
protocol_version
type
id
timestamp
payload
```

R4 remains additive on protocol version 1.

## Connection lifecycle

Runner → Controller:

- `HOST_REGISTER`
- `RUNNING_JOBS`
- `HEARTBEAT`

After every reconnect the Runner sends `RUNNING_JOBS` before buffered results.

Example:

```json
{
  "type": "RUNNING_JOBS",
  "payload": {
    "host_id": "desktop-main",
    "running_jobs": [
      {"execution_id": "abc", "session_id": "thread-1"}
    ],
    "completed_jobs": [
      {"execution_id": "def", "session_id": "thread-2"}
    ]
  }
}
```

Heartbeat also carries the current running execution snapshot.

## Controller → Runner

- `JOB_START`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_CANCEL`

Human Gate decisions and recovery resumes reuse `JOB_RESUME`.

## Runner → Controller

- `JOB_ACCEPTED`
- `JOB_PROGRESS`
- `JOB_RESULT`
- `JOB_ERROR`
- `SESSION_STARTED`
- `HUMAN_GATE`

A `JOB_RESULT` may additionally contain:

```text
retry_kind
retry_at
```

R4 uses `retry_kind=host` or `retry_kind=quota` as typed recovery hints. The Controller still owns the resulting Job state transition.

## Restart adoption

The Controller persists remote `execution_id` while a Job is active. When `RUNNING_JOBS` reports the same id after Controller restart, `RunnerGateway.adopt_remote()` recreates the Controller-side waiter without dispatching a duplicate Codex command.

If no matching execution is reported, normal recovery resume occurs after the restart grace period.

## Human Gate

`HUMAN_GATE` remains the structured approval event introduced in R3.

The Controller, not the Runner, persists the Approval and owns `WAITING_HUMAN`.

## Reserved

`JOB_PAUSE` remains reserved as a dedicated transport primitive. Current pause cancels the active turn while preserving session state.
