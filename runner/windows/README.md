# Windows Runner

The Windows Runner is available from R1.

It creates an outbound WebSocket to the Controller. Do not open an inbound Desktop port.

Run `runner/windows/install.ps1`, configure the `REMOTE_RUNNER_*` settings in `.env`, authenticate Codex CLI on the Windows account, then run:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

The Runner registers itself and sends heartbeats automatically.


## UTF-8 / Korean text

Remote Runner starts npm `.CMD` wrappers on Windows code page 65001 and launches Codex with UTF-8-oriented child settings. It also adds a small runtime instruction telling Codex to avoid legacy PowerShell text decoding paths for repository files.

PowerShell 7 (`pwsh`) is strongly recommended when available. Windows PowerShell 5.1 can decode BOM-less UTF-8 files through the system ANSI code page (for example CP949 on Korean Windows) when commands such as `Get-Content` are used without an explicit encoding. The Runner therefore tells Codex to prefer `rg`, `git`, or explicit UTF-8 reads and to use `-Encoding UTF8` when a PowerShell text cmdlet is unavoidable.

The original Telegram/job instruction itself is written to Codex stdin as UTF-8; Korean text does not need a second retry solely to recover from shell mojibake.
