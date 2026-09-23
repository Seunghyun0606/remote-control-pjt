$ErrorActionPreference = "Stop"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11+ is required."
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
