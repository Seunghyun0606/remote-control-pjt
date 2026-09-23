# Sessions and Feedback

## Session Registry

A session record stores:

```text
internal session id
job id
project id
host id
agent type
external Codex session/thread id
status
created time
last active time
```

Session status values through R3:

- `ACTIVE`
- `WAITING_HUMAN`
- `PAUSED`
- `IDLE`
- `FAILED`
- `CANCELLED`

## Resume strategy

Preferred path:

```text
existing external session
  ↓
codex exec resume
```

Fallback:

```text
resume unavailable / failed
  ↓
reload repository state
  ↓
new codex exec session
```

A Human Gate decision uses the same resume strategy. The decision is sent as a new turn; it is not injected into a process stdin mid-turn.

## Steering

R2 steering is written to the Event Ledger and applied at a safe turn boundary.

## Feedback levels

The design vocabulary is:

- `DEBUG`
- `PROGRESS`
- `IMPORTANT`
- `HUMAN_REQUIRED`
- `FINAL`

R3 implements Human Gate classification as `HUMAN_REQUIRED`. Unlike ordinary progress, it is sent immediately through the Approval notification path.

Default progress interval:

```text
300 seconds
```

Configure with:

```dotenv
REMOTE_CONTROL_PROGRESS_INTERVAL_SECONDS=300
```

Low-level command events are not copied wholesale to Telegram. Completion/failure and Human Gate events are handled separately from throttled progress.
