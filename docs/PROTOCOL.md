# Runner Protocol

Remote transport starts in Phase R1. The protocol is documented now so R0 does not couple the core to a local process.

Envelope:

```json
{
  "protocol_version": 1,
  "type": "JOB_START",
  "id": "message-id",
  "timestamp": "2026-09-23T00:00:00Z",
  "payload": {}
}
```

Reserved message types:

- `HOST_REGISTER`
- `HEARTBEAT`
- `JOB_START`
- `JOB_CANCEL`
- `JOB_PAUSE`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_ACCEPTED`
- `JOB_PROGRESS`
- `JOB_RESULT`
- `JOB_ERROR`
- `SESSION_STARTED`
- `HUMAN_GATE`

R0 does not expose `/ws/runner`; the local runner is invoked through the same conceptual boundary.
