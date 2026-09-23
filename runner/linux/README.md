# Linux Runner

The Controller host normally executes Codex locally through `CodexRunner`.

A second Linux machine can also use the standalone `remote-runner` introduced in R1. Configure its `REMOTE_RUNNER_*` settings and run:

```bash
remote-runner start
```

Like the Windows Runner, it opens an outbound WebSocket and uses its own local Codex authentication.
