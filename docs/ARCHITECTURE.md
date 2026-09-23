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

## Primary deployment model

The normal deployment is one independent Controller per execution machine.

```text
Telegram/Slack A -> Desktop Controller -> Desktop Codex -> Desktop projects
Telegram/Slack B -> Lightsail Controller -> Lightsail Codex -> Lightsail projects
```

Desktop and Lightsail do not need to control each other. Each node owns its local runtime DB, project registry and Codex authentication.

The remote Runner path remains available as an advanced topology when one Controller intentionally needs to dispatch to another machine.

## R0 ~ R6 runtime internals

```text
Telegram / Slack
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

Browser -> FastAPI /ui -> DashboardService (read-only)
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

## R6 messaging and dashboard

Messaging providers register notification callbacks by channel. Telegram and Slack can run simultaneously without replacing each other's notifier.

Slack uses outbound Socket Mode. Provider-specific payloads stop at the messaging boundary; ControllerService receives normalized text plus channel/user identity.

DashboardService aggregates runtime repositories for `GET /dashboard` and the optional `GET /ui` page. It is read-only and does not mutate Job or Project OS state.

## Roadmap state

R0 through R6 are implemented. Future messaging or gateway integrations should continue to depend on MessagingProvider and ControllerService rather than moving vendor logic into JobManager.
