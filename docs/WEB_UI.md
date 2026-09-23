# Web Dashboard

R6 adds a small optional read-only Web dashboard.

## Endpoints

```text
GET /dashboard
GET /ui
```

`/dashboard` returns a JSON snapshot.

`/ui` renders that snapshot and refreshes every five seconds.

Disable only the HTML UI with:

```dotenv
REMOTE_CONTROL_WEB_UI_ENABLED=false
```

The JSON dashboard remains available for observability integrations.

## Displayed state

Dashboard metrics:

- projects
- active_hosts
- active_jobs
- queued_jobs
- failed_jobs
- average_job_duration_seconds
- quota_wait_count
- human_gate_count
- recovery_count

Tables:

- Host status/capabilities
- recent Jobs
- Project/Task/Host binding
- pending Human Gates

## Safety boundary

The R6 HTML dashboard is intentionally read-only.

It does not provide buttons that:

- run a project
- steer an Agent
- resolve an Approval
- cancel a Job
- execute shell commands

Existing API mutation endpoints remain separate Controller interfaces.

The HTTP server binds to `127.0.0.1` by default. Do not expose it directly to the public Internet. Use a private network or an authenticated reverse proxy if remote browser access is required.

## Architecture

```text
Browser
  ↓ GET /ui
FastAPI
  ↓
DashboardService
  ├─ ProjectRegistry
  ├─ HostRegistry
  ├─ JobRepository
  ├─ ProjectWorkRepository
  ├─ ApprovalRepository
  └─ RecoveryRepository
```

DashboardService reads runtime state only. It does not mutate Project OS canonical state.
