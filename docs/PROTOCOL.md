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

R3 continues protocol version 1.

## Host lifecycle

Runner → Controller:

- `HOST_REGISTER`
- `HEARTBEAT`

## Controller → Runner

- `JOB_START`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_CANCEL`

`JOB_RESUME` and `JOB_STEER` both carry the external session id, instruction and working directory.

R3 Human Gate decisions reuse `JOB_RESUME`: the Controller sends the human decision as the next instruction on the same session when possible.

## Runner → Controller

- `JOB_ACCEPTED`
- `JOB_PROGRESS`
- `JOB_RESULT`
- `JOB_ERROR`
- `SESSION_STARTED`
- `HUMAN_GATE`

A `HUMAN_GATE` message is associated with the active execution id and may carry:

```json
{
  "type": "HUMAN_GATE",
  "execution_id": "...",
  "session_id": "...",
  "event": {
    "type": "HUMAN_GATE",
    "approval_type": "architecture_change",
    "question": "Proceed with schema v3?",
    "details": "Persistent data changes are required.",
    "options": [
      {"key": "A", "label": "Keep v2"},
      {"key": "B", "label": "Migrate to v3"}
    ]
  }
}
```

The Controller, not the Runner, persists the Approval and owns the `WAITING_HUMAN` state transition.

Reserved for later:

- `JOB_PAUSE` as a dedicated transport primitive
- running-job reconciliation messages for R4
