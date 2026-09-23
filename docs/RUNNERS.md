# Runners

`AgentRunner` is the Controller-side boundary for coding-agent execution.

Runner operations:

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

R3 appends a Remote Control Human Gate protocol to the agent instruction. If a human decision is required, the agent is asked to finish a safe step, emit the marker/JSON request, and stop the turn.

## HybridAgentRunner

- Controller local Host → local `CodexRunner`
- remote Host → `RunnerGateway`

Resume, steering and Human Gate continuation keep the same Host because Codex session files/authentication live on that execution Host.

## Remote Runner

The standalone `remote-runner` accepts:

- `JOB_START`
- `JOB_RESUME`
- `JOB_STEER`
- `JOB_CANCEL`

It can emit:

- `JOB_PROGRESS`
- `SESSION_STARTED`
- `HUMAN_GATE`
- `JOB_RESULT`
- `JOB_ERROR`

The Runner sanitizes the event before transport. Human Gate question/details/options are retained; broad raw agent output is not forwarded wholesale.

Codex credentials are never transported through the WebSocket protocol.
