# Architecture

## Responsibility split

Remote Agent Control stores runtime state only:

- Jobs
- Hosts
- Codex Sessions
- Runtime Events
- Feedback delivery state

Project/product state remains outside this control plane.

## R0 + R1 + R2

```text
Telegram
  |
ControllerService -> CommandRouter
  |
  +----> HostRegistry / HostRouter
  +----> SessionRegistry
  |
JobManager
  |
HybridAgentRunner
  |                         ^
  | local                   | outbound WebSocket
CodexRunner             Desktop Runner
  |                         |
Codex CLI               Codex CLI
```

## Session lifecycle

```text
new Job
  ↓
codex exec
  ↓
thread.started
  ↓
SessionRegistry

pause
  ↓
process stop + session preserved
  ↓
PAUSED

resume / steering
  ↓
codex exec resume <external_session_id>
  ↓
same thread preferred
  ↓ fail
repository state reload
  ↓
new Codex session
```

Codex session is an optimization, not Source of Truth.

## Steering

R2 uses safe turn-boundary steering.

```text
Messenger instruction
  ↓
STEERING_QUEUED event
  ↓
current Codex turn completes
  ↓
JOB_STEER
  ↓
same session resume
```

This avoids terminating Codex while it may be writing project files.

## Feedback

Raw agent events are stored as runtime events. Messenger receives only selected feedback. Progress is throttled by configuration; important/final events are not treated as raw log streaming.

## Planned phases

- R3: Human Gate approvals
- R4: scheduler, quota/host waits and restart reconciliation
- R5: Project OS adapter through `projectctl`
- R6: Slack and optional Web UI
