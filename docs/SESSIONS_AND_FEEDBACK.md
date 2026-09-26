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


## Session history commands

Messenger session metadata can be queried with:

```text
/sessions
/session SESSION-...
```

Project Topics scope `/sessions` to the current project, and ownership checks ensure users only see Sessions attached to their own Jobs.

The Session record stores metadata and the Codex external session ID, not a duplicate full transcript. Codex remains the source of truth for the actual conversation history.


## Steering vs immediate redirect

Remote Control provides two different ways to change direction during a running Job.

`/steer [--job <job-id>] <instruction>` is non-interrupting. The instruction is queued, the current Codex turn finishes, and the instruction is applied as the next turn in the same Codex session.

`/redirect [--job <job-id>] <instruction>` is interrupting. Remote Control uses the existing safe pause/cancel path to stop the current turn, waits for that turn to settle, and immediately resumes the same Codex session with the replacement instruction.

Use `/steer` when the current work is still useful. Use `/redirect` when continuing the current direction would waste time or make unwanted changes.

## Queued Jobs and Project Sessions

A `WAITING_LEASE` Job does not lock a Project Session while it waits. When the checkout becomes free, Remote Control reacquires the user's current Project Session and refreshes the Job's Codex thread binding before execution. This allows several explicit `/run` requests to wait safely while still preserving the latest session context.
