# Runners

`AgentRunner` is the Controller-side boundary for coding-agent execution.

Operations:

- `start`
- `resume`
- `steer`
- `cancel`

## CodexRunner

New session:

```text
codex exec --json ...
```

Resume:

```text
codex exec resume <SESSION_ID> --json ...
```

Instructions are passed through stdin rather than shell interpolation.

CodexRunner classifies quota signals into typed `AgentRunResult` recovery hints. A structured reset timestamp is retained when available.

## HybridAgentRunner

- Controller local Host → local `CodexRunner`
- remote Host → `RunnerGateway`

Session resume stays on the same assigned Host unless HostRouter is performing an `auto` recovery decision.

## Desktop Runner reconnect behavior

The standalone Runner keeps its Codex executions independent from the Controller WebSocket.

When the WebSocket is lost:

- the Runner process stays alive
- active Codex handles stay in the Runner
- progress delivery is temporarily skipped
- completed results are buffered in memory

When it reconnects:

1. `HOST_REGISTER`
2. `RUNNING_JOBS`
3. buffered `JOB_RESULT` delivery
4. normal heartbeat/event flow

This permits a restarted Controller to adopt an existing remote execution instead of starting a duplicate task.

If the Runner process itself is restarted, in-memory execution tracking is lost. Recovery then uses the persisted Codex session/repository strategy.

## Credentials

Codex credentials stay local to each execution Host and are never transported in Runner protocol payloads.
