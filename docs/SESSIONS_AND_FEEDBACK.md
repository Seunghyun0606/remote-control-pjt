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

Session status values in R2:

- `ACTIVE`
- `PAUSED`
- `IDLE`
- `FAILED`
- `CANCELLED`

The external Codex thread id is learned from the JSON event stream and stored as soon as it is available.

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

If Codex returns a different thread id than the requested id, the Controller records a session rebound and adopts the returned thread instead of pretending the original thread was resumed.

## Steering

A steering message is written to the Event Ledger immediately. R2 does not inject text into a currently executing Codex process.

Instead:

1. finish the current turn
2. combine pending steering messages
3. resume the same session
4. send the steering instruction through the Runner

For a remote Host this uses `JOB_STEER`.

## Feedback levels

The design vocabulary is:

- `DEBUG`
- `PROGRESS`
- `IMPORTANT`
- `HUMAN_REQUIRED`
- `FINAL`

R2 implements progress and important event classification. Human-required feedback is added in R3.

Default progress interval:

```text
300 seconds
```

Configure with:

```dotenv
REMOTE_CONTROL_PROGRESS_INTERVAL_SECONDS=300
```

Low-level command events are not copied wholesale to Telegram. Agent messages may become throttled progress updates. Command failures are treated as important signals.

Completion/failure is sent separately as final Job feedback.
