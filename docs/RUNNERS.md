# Runners

`AgentRunner` is the Controller-side boundary for coding-agent execution.

R1 ships:

- `CodexRunner`: local Codex CLI.
- `HybridAgentRunner`: selects local Codex or remote WebSocket execution.
- `FakeAgentRunner`: deterministic test runner.
- `remote-runner`: standalone Desktop/Linux host daemon.

The remote Runner opens an outbound WebSocket, registers host metadata/capabilities, sends heartbeats, accepts validated jobs, starts local Codex CLI, and streams results back.

The Runner does not receive Codex authentication from the Controller. Codex authentication stays local to each Host.

Codex instructions are passed through stdin rather than shell interpolation.

R2 will add session resume and steering while preserving this boundary.
