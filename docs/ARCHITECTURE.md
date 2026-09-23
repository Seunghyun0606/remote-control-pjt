# Architecture

## Responsibility split

Remote Agent Control stores runtime state only: Jobs, Host state, agent process/session metadata and runtime events. Project/product state remains outside this control plane.

## R0 + R1

```text
Telegram
  |
ControllerService -> CommandRouter
  |
  +----> HostRegistry / HostRouter ----> SQLite
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

The Telegram layer never invokes a shell or Codex directly. The Desktop Runner never exposes an inbound listener; it creates the persistent outbound connection to the Controller.

## Local vs remote execution

`HybridAgentRunner` preserves one Controller-side `AgentRunner` abstraction.

- assigned host == Controller local host -> `CodexRunner`
- other connected host -> `RunnerGateway` -> WebSocket Runner

## Runtime persistence

SQLite contains jobs, events and hosts. Host heartbeat is runtime state and is not written into a project repository or Project OS state.

## Planned phases

- R2: Codex session registry, resume, steering and throttled progress
- R3: Human Gate approvals
- R4: scheduler, quota/host waits and restart reconciliation
- R5: Project OS adapter through `projectctl`
- R6: Slack and optional Web UI
