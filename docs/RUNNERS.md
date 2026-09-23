# Runners

`AgentRunner` is the boundary between the Controller and a coding agent.

R0 ships:

- `CodexRunner`: launches local Codex CLI on the Controller/Lightsail host.
- `FakeAgentRunner`: deterministic runner for automated tests.

Codex input is sent through stdin, not shell interpolation.

The executable uses a command equivalent to:

```text
codex exec --json --sandbox workspace-write --cd <project-dir> --config approval_policy="never" -
```

R1 will add the host daemon/WebSocket protocol while preserving this runner boundary.
