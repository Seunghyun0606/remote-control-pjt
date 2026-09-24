$ErrorActionPreference = "Stop"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11+ is required."
}

if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
    Write-Warning "PowerShell 7 (pwsh) is recommended for reliable UTF-8/Korean text handling. The Runner includes UTF-8 safeguards, but legacy Windows PowerShell 5.1 still has known ANSI text-decoding behavior."
}

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env. Configure REMOTE_RUNNER_* values before starting."
}

Write-Host "Install complete."
Write-Host "Authenticate Codex CLI on this Windows account, then run:"
Write-Host ".\.venv\Scripts\remote-runner.exe start"
