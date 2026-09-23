# Runners

`AgentRunner` is the Controller-side boundary for coding-agent execution.

R2 runner operations:

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

## HybridAgentRunner

- Controller local Host → local `CodexRunner`
- remote Host → `RunnerGateway`

Resume and steering keep the same Host because Codex session files/authentication live on that execution Host.

## Remote Runner

The standalone `remote-runner` accepts:

- `JOB_START`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_CANCEL`

It uses the Host's own Codex authentication and sends sanitized structured events back to the Controller.

Codex credentials are never transported through the WebSocket protocol.
