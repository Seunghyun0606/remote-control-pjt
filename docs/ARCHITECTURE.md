# Architecture

## Responsibility split

Remote Agent Control stores only runtime state: jobs, agent process/session metadata, runtime events and later host heartbeats/approvals.

Project/product state remains outside this control plane. Project OS integration will be an optional adapter in Phase R5.

## R0

```text
Telegram
  |
  v
MessagingProvider
  |
  v
ControllerService -> CommandRouter
  |
  v
JobManager -> SQLite event/job store
  |
  v
AgentRunner (CodexRunner)
  |
  v
Codex CLI on the same Lightsail host
```

The Telegram layer never invokes a shell or Codex directly.

## Planned phases

- R1: outbound WebSocket Desktop Runner, host registry and heartbeat
- R2: Codex session registry, resume, steering and throttled progress
- R3: Human Gate approvals
- R4: scheduler, quota/host waits and restart reconciliation
- R5: Project OS adapter through `projectctl`
- R6: Slack and optional Web UI
