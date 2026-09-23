# Architecture

## Responsibility split

Remote Agent Control stores runtime state only:

- Jobs
- Hosts
- Codex Sessions
- Approvals
- Recovery metadata
- Runtime Events
- Feedback delivery state

Project/product state remains outside this control plane.

## R0 ~ R4

```text
Telegram
  |
ControllerService -> CommandRouter
  |
  +----> HostRegistry / HostRouter
  +----> SessionRegistry
  +----> ApprovalRegistry
  +----> RecoveryRepository
  |
JobManager <---- RecoveryScheduler
  |
HybridAgentRunner
  |                         ^
  | local                   | outbound WebSocket
CodexRunner             Desktop Runner
  |                         |
Codex CLI               Codex CLI
```

## Runtime state

The Job state machine is code-controlled. Relevant recovery states are:

```text
WAITING_HUMAN
WAITING_HOST
WAITING_QUOTA
PAUSED
```

The LLM may emit a signal, but it does not mutate Job state directly.

## Session strategy

Preferred continuation:

```text
external Codex session
  ↓
codex exec resume
```

Fallback:

```text
session unavailable
  ↓
reload repository state
  ↓
new Codex session
```

Codex session is an optimization. Repository/filesystem state remains the durable work state.

## Host recovery

```text
Host unavailable
  ↓
WAITING_HOST
  ↓
Scheduler / heartbeat
  ↓
HostRouter reevaluation
  ↓
ASSIGNED
  ↓
STARTING / RUNNING
```

For `auto` routing, another compatible configured Host may be selected. An explicit Host remains pinned to that Host.

## Quota recovery

```text
quota signal
  ↓
WAITING_QUOTA
  ↓
RecoveryRecord.next_retry_at
  ↓
Scheduler
  ↓
same session resume
```

Retry attempt count is persisted so repeated quota events increase the backoff until the configured cap.

## Controller restart reconciliation

Remote executions have an `execution_id` persisted in Recovery state.

```text
Controller restart
  ↓
active DB jobs → WAITING_HOST
  ↓
remote Runner reconnects
  ↓
RUNNING_JOBS
  ↓
execution_id match?
  ├─ yes → adopt existing RemoteRunHandle
  └─ no  → grace expires → session/repository resume
```

Local Controller executions cannot survive the Controller process itself. They use session/repository resume.

## Scheduler

The R4 scheduler periodically:

- expires stale Host heartbeats
- expires approvals that have `expires_at`
- retries `WAITING_HOST`
- retries `WAITING_QUOTA`

The current product does not expose a user-facing arbitrary scheduled-job submission model; the R4 Scheduler is the runtime recovery scheduler.

## Planned phases

- R5: Project OS adapter through `projectctl`
- R6: Slack and optional Web UI
