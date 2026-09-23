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

R2 still uses protocol version 1.

## Host lifecycle

Runner → Controller:

- `HOST_REGISTER`
- `HEARTBEAT`

## Controller → Runner

- `JOB_START`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_CANCEL`

`JOB_RESUME` and `JOB_STEER` both carry:

- execution id
- external session id
- instruction
- working directory

The difference is intent: resume continues a paused Job; steer applies user feedback at the next safe turn boundary.

Reserved for later:

- `JOB_PAUSE` as a transport-level primitive

R2 pause currently uses `JOB_CANCEL` for the active process while keeping the Controller Job as `PAUSED` and preserving the Session Registry entry.

## Runner → Controller

- `JOB_ACCEPTED`
- `JOB_PROGRESS`
- `JOB_RESULT`
- `JOB_ERROR`
- `SESSION_STARTED`

Remote progress carries a sanitized subset of the Codex JSON event, enough for session discovery and feedback classification without forwarding the entire raw output to Messenger.

R3 adds `HUMAN_GATE`.
