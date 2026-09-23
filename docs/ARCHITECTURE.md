# Architecture

## Responsibility split

Remote Agent Control stores runtime state only:

- Jobs
- Hosts
- Codex Sessions
- Approvals
- Runtime Events
- Feedback delivery state

Project/product state remains outside this control plane.

## R0 + R1 + R2 + R3

```text
Telegram
  |
ControllerService -> CommandRouter
  |
  +----> HostRegistry / HostRouter
  +----> SessionRegistry
  +----> ApprovalRegistry
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

## Human Gate lifecycle

```text
RUNNING
   ↓
agent reports HUMAN_GATE
   ↓
ApprovalRegistry
   ↓
WAITING_HUMAN
   ↓
active non-interactive turn stops
   ↓
Telegram inline options / text choice / Reject
   ↓
HUMAN_GATE_RESOLVED
   ↓
RUNNING
   ↓
same Host + same Codex session resume
```

The Controller owns the runtime approval state. It does not depend on a particular Codex internal approval API.

For `codex exec --json`, the instruction contains a Remote Control marker contract. A Runner may also provide an explicit structured `HUMAN_GATE` event. Both normalize to the same Approval Registry.

## Steering

R2 uses safe turn-boundary steering. Human decisions similarly create a new turn rather than injecting stdin into an agent process that may be writing files.

## Feedback

Raw agent events are stored as runtime events. Messenger receives selected feedback. Human Gate is always surfaced immediately and is not progress-throttled.

## Planned phases

- R4: scheduler, quota/host waits and restart reconciliation
- R5: Project OS adapter through `projectctl`
- R6: Slack and optional Web UI
