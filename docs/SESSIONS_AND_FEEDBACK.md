# Sessions and Feedback

## Session Registry

Session status values through R4:

- `ACTIVE`
- `WAITING_HUMAN`
- `WAITING_HOST`
- `WAITING_QUOTA`
- `PAUSED`
- `IDLE`
- `FAILED`
- `CANCELLED`

The external Codex thread/session id is learned from the event stream or result and persisted as runtime metadata.

## Resume strategy

Preferred:

```text
existing external session
  ↓
codex exec resume
```

Fallback:

```text
session unavailable
  ↓
reload repository state
  ↓
new codex exec session
```

The same strategy is reused after pause, Human Gate, quota wait, Host wait and Controller restart.

## Remote execution identity

R4 additionally persists remote `execution_id` in Recovery state. This identifies an active Runner execution and is separate from the Codex session id.

- `execution_id`: Controller ↔ Runner execution correlation
- external session id: Codex continuation identity

A Controller restart may adopt the former. If it cannot, it resumes through the latter.

## Feedback

Feedback vocabulary remains:

- `DEBUG`
- `PROGRESS`
- `IMPORTANT`
- `HUMAN_REQUIRED`
- `FINAL`

Progress is throttled. Human Gate, quota wait/resume, Host wait/resume and final state changes are explicit runtime events and may generate direct user notifications.
