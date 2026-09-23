# Windows Runner

The Windows Runner is available from R1.

It creates an outbound WebSocket to the Controller. Do not open an inbound Desktop port.

Run `runner/windows/install.ps1`, configure the `REMOTE_RUNNER_*` settings in `.env`, authenticate Codex CLI on the Windows account, then run:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

The Runner registers itself and sends heartbeats automatically.
