# Remote Agent Control

Messenger에서 Lightsail/Desktop의 AI coding agent를 안전하게 실행하고, 진행 상태·추가 지시·Human Gate·장애 복구까지 원격으로 관리하는 Runtime Control Plane입니다.

Project OS와는 분리되어 있습니다.

- **Project OS**: 무엇을 해야 하는지, 장기 프로젝트 상태
- **Remote Agent Control**: 어디서/어떻게 실행하는지, Job/Host/Session/Approval/Recovery runtime 상태
- **Codex**: 실제 작업 수행

현재 **R0 ~ R4**까지 구현되어 있습니다.

```text
Telegram
   ↓
Controller
   ├─ Job / Event / Host / Session / Approval / Recovery
   ├─ Recovery Scheduler
   ├─ Lightsail Local Runner → Codex CLI
   └─ WebSocket Gateway
            ↑ outbound only
       Desktop Runner → Codex CLI
```

R4에서 추가된 기능:

- `WAITING_HOST` 자동 복구
- Host heartbeat expiry → persisted OFFLINE
- auto routing 시 compatible online Host fallback
- `WAITING_QUOTA` + 자동 retry
- quota retry 기본 30분, 최대 2시간 exponential backoff
- 구조화된 quota reset 시간이 있으면 해당 시각 우선 사용
- Controller restart 시 active Job reconciliation
- Desktop Runner `RUNNING_JOBS` 보고
- 살아 있는 remote execution을 새 Controller가 adopt
- remote execution이 없으면 기존 Codex session/repository state 기반 resume
- Approval `expires_at` 자동 처리
- runtime Recovery Registry / `GET /recovery`
- package version `0.5.0`

## Quick Start

Python 3.11+, Git, Codex CLI가 필요합니다.

```bash
git clone https://github.com/Seunghyun0606/remote-control-pjt.git
cd remote-control-pjt

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

주요 설정:

```dotenv
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_HOST_ID=lightsail-main

REMOTE_CONTROL_HEARTBEAT_TIMEOUT_SECONDS=45
REMOTE_CONTROL_PROGRESS_INTERVAL_SECONDS=300
REMOTE_CONTROL_SCHEDULER_INTERVAL_SECONDS=15

REMOTE_CONTROL_QUOTA_RETRY_INITIAL_SECONDS=1800
REMOTE_CONTROL_QUOTA_RETRY_MAX_SECONDS=7200
REMOTE_CONTROL_RESTART_GRACE_SECONDS=10

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789

CONTROLLER_RUNNER_TOKEN=<long-random-secret>
```

Controller:

```bash
remote-control controller start
```

Telegram 없이 API만 테스트:

```bash
remote-control controller start --no-telegram
```

Codex 인증정보는 각 실행 Host의 로컬 계정에만 둡니다. Controller DB로 복사하거나 WebSocket으로 전송하지 않습니다.

## 기본 사용

```text
/projects
/hosts
/run dailytown
/run dailytown --host desktop-main
/status
/jobs
/job JOB-...
```

일시정지 / 재개:

```text
/pause
/resume
```

추가 지시:

```text
/steer UI는 건드리지 말고 backend만 수정해
```

active Job이 하나라면 일반 텍스트도 steering으로 사용할 수 있습니다.

Human Gate가 발생하면:

```text
⚠ Human Gate

Save Schema v3 migration을 허용할까요?

[A · 기존 Schema 유지] [B · v3 Migration 진행]
[Details] [Reject]
```

선택 후 기존 Host/Codex session을 우선 사용해 이어서 실행합니다.

중지:

```text
/stop
/stop JOB-...
```

## R4 장애 복구

### Host offline

명시한 Host가 offline이면 Job을 실패시키지 않고:

```text
QUEUED / RUNNING / PAUSED
          ↓
    WAITING_HOST
          ↓ heartbeat / reconnect
       ASSIGNED
          ↓
       RUNNING
```

`--host desktop-main`처럼 Host를 명시했다면 그 Host가 돌아올 때까지 기다립니다.

`--host auto`인 경우 등록된 Project path와 allowed host 안에서 online Host를 다시 평가합니다. Default Host가 offline이고 compatible fallback Host가 online이면 fallback Host를 사용할 수 있습니다.

### Codex quota

Quota/usage-limit 신호를 감지하면:

```text
RUNNING
   ↓
WAITING_QUOTA
   ↓ Scheduler
retry
   ↓
RUNNING
```

reset 시각을 구조화된 이벤트에서 얻을 수 있으면 그 시각을 사용합니다. 그렇지 않으면 기본적으로 30분 → 1시간 → 2시간의 backoff를 사용하고 2시간에서 cap합니다.

### Controller restart

Controller가 재시작되면 DB의 active runtime Job을 먼저 복구 대상으로 전환합니다.

Remote Desktop Runner가 계속 살아 있다면 reconnect 직후 `RUNNING_JOBS`를 보고하고 새 Controller가 기존 `execution_id`를 adopt합니다.

```text
Controller A dies
      ↓
Desktop Codex continues
      ↓
Controller B starts
      ↓
WAITING_HOST + persisted execution_id
      ↓
Runner reconnects / RUNNING_JOBS
      ↓
same execution adopted
```

기존 execution이 더 이상 존재하지 않으면 grace 이후 기존 Codex session을 우선 resume하고, session resume가 불가능하면 기존 R2 정책대로 repository filesystem 상태를 다시 읽어 새 session으로 이어갑니다.

## Desktop Runner

Windows:

```powershell
.\runner\windows\install.ps1
```

`.env`:

```dotenv
REMOTE_RUNNER_CONTROLLER_WS=wss://YOUR-CONTROLLER/ws/runner
REMOTE_RUNNER_TOKEN=<controller와 동일한 transport secret>
REMOTE_RUNNER_HOST_ID=desktop-main
REMOTE_RUNNER_NAME=Main Desktop
REMOTE_RUNNER_OS=windows
REMOTE_RUNNER_CAPABILITIES=codex,git,android,gui,browser
```

실행:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

Desktop Runner는 Controller로 outbound WebSocket만 연결합니다.

## 프로젝트 등록

`config/projects.yaml`:

```yaml
projects:
  dailytown:
    name: DailyTown
    adapter: generic_git
    repository:
      path:
        lightsail-main: /home/codex/projects/dailytown
        desktop-main: C:/dev/dailytown
    allowed_hosts:
      - lightsail-main
      - desktop-main
    default_host: lightsail-main
```

Messenger에서 임의 filesystem path를 전달할 수 없습니다.

## Runtime API

주요 조회 API:

```text
GET /health
GET /projects
GET /hosts
GET /jobs
GET /sessions
GET /approvals
GET /recovery
```

HTTP API는 기본적으로 `127.0.0.1`에 bind합니다.

## 상세 문서

- [Architecture](docs/ARCHITECTURE.md)
- [Recovery & Scheduler](docs/RECOVERY.md)
- [Sessions and Feedback](docs/SESSIONS_AND_FEEDBACK.md)
- [Human Gate](docs/HUMAN_GATE.md)
- [Messaging](docs/MESSAGING.md)
- [Runners](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)
- [Security](docs/SECURITY.md)
- [R2 Manual Smoke Test](docs/SMOKE_TEST_R2.md)
- [R3 Manual Smoke Test](docs/SMOKE_TEST_R3.md)
- [R4 Manual Smoke Test](docs/SMOKE_TEST_R4.md)

## Roadmap

- [x] R0 — Telegram + Lightsail Codex
- [x] R1 — Desktop Runner / WebSocket / heartbeat
- [x] R2 — Session resume / steering / progress feedback
- [x] R3 — Human Gate
- [x] R4 — retry / quota / restart recovery
- [ ] R5 — Project OS adapter
- [ ] R6 — Slack / Web UI
