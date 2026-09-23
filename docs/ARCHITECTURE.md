# Architecture

## Responsibility split

Remote Agent Control stores runtime state:

- Jobs
- Hosts
- Codex Sessions
- Approvals
- Recovery metadata
- ProjectWork execution bindings
- Runtime Events
- Feedback delivery state

Project/product state remains outside this control plane. When Project OS is used, its canonical state stays under `.project-os` and is accessed through `projectctl`.

## R0 ~ R5

```text
Telegram
  |
ControllerService -> CommandRouter
  |
  +----> HostRegistry / HostRouter
  +----> SessionRegistry
  +----> ApprovalRegistry
  +----> RecoveryRepository
  +----> ProjectWorkRepository
  |
JobManager <---- RecoveryScheduler
  |
  +----> ProjectAdapterRegistry
  |       ├─ GenericGitAdapter
  |       └─ ProjectOSAdapter
  |              |
  |         ProjectOperationExecutor
  |           /             \
  |       local           remote RPC
  |        |                 |
  |     git/projectctl   Desktop Runner
  |
HybridAgentRunner
  |                         ^
  | local                   | outbound WebSocket
CodexRunner             Desktop Runner
  |                         |
Codex CLI               Codex CLI
```

Dependency direction is one-way:

```text
Remote Control
   ↓
Project OS Adapter
   ↓
projectctl
   ↓
Project OS canonical state
```

Project OS does not import or depend on Remote Control.

## Project adapters

The adapter is selected from the server-side Project Registry.

`generic_git` builds an execution instruction from Git state and has no Project OS dependency.

`project_os` performs deterministic Task selection/context/claim before Codex and submits the implementation handoff after Codex succeeds.

The LLM does not select its own Task and does not directly mutate Project OS state.

## ProjectWork

ProjectWork is runtime metadata linking one Remote Job to one Project OS task and Host.

It exists to support:

- deterministic continuation
- host pinning
- claim reconciliation
- result-submission recovery
- diagnostics

It is not the long-term project plan.

## Runtime state

Relevant wait states:

```text
WAITING_AGENT   adapter finalization
WAITING_HUMAN   human decision
WAITING_HOST    execution/project Host unavailable
WAITING_QUOTA   Codex quota
PAUSED
```

State transitions are code-controlled.

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

Codex session is an optimization. Repository/filesystem state remains the durable implementation state.

## R5 finalization recovery

After Codex succeeds for a Project OS task:

```text
RUNNING
  ↓
WAITING_AGENT
  ↓ projectctl submit
COMPLETED
```

Controller restart during this phase is reconciled as `RecoveryMode.FINALIZE`. It retries adapter submission instead of rerunning the Agent.

Project OS task approval remains separate.

## Host recovery

Before Project OS Task selection, `auto` routing may select any configured online Host.

After a ProjectWork binding exists, recovery is pinned to its `host_id` to avoid continuing a claimed canonical Task against another checkout.

## Scheduler

The recovery scheduler handles:

- stale Host heartbeat expiry
- Human Gate expiry
- `WAITING_HOST`
- `WAITING_QUOTA`
- Project OS `FINALIZE` recovery through the Host retry path

## Planned phase

- R6: Slack and optional Web UI
