# Remote Agent Control

Messenger에서 Lightsail/Desktop의 AI coding agent를 안전하게 실행하고, 진행 상태·추가 지시·Human Gate·장애 복구·Project OS 연동까지 원격으로 관리하는 Runtime Control Plane입니다.

세 역할은 분리합니다.

- **Project OS**: 무엇을 해야 하는지, Task/Context/Decision 등 장기 프로젝트 상태
- **Remote Agent Control**: 어디서/어떻게 실행하는지, Job/Host/Session/Approval/Recovery runtime 상태
- **Codex**: 실제 구현 작업 수행

현재 **R0 ~ R5**까지 구현되어 있습니다.

```text
Telegram
   ↓
Controller
   ├─ Job / Host / Session / Approval / Recovery / ProjectWork
   ├─ Recovery Scheduler
   ├─ ProjectAdapter
   │    ├─ Generic Git
   │    └─ Project OS → projectctl
   ├─ Lightsail Local Runner → Codex CLI
   └─ WebSocket Gateway
            ↑ outbound only
       Desktop Runner → Codex CLI / projectctl
```

## Quick Start

Python 3.11+, Git, Codex CLI가 필요합니다. Project OS 프로젝트를 실행하는 Host에는 `projectctl`도 설치되어 있어야 합니다.

```bash
git clone https://github.com/Seunghyun0606/remote-control-pjt.git
cd remote-control-pjt

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

필수/주요 설정:

```dotenv
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_HOST_ID=lightsail-main

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
CONTROLLER_RUNNER_TOKEN=<long-random-secret>

CODEX_EXECUTABLE=codex
PROJECTCTL_EXECUTABLE=projectctl
GIT_EXECUTABLE=git
```

Controller:

```bash
remote-control controller start
```

Telegram 없이 API만:

```bash
remote-control controller start --no-telegram
```

## 프로젝트 등록

### 일반 Git 프로젝트

```bash
remote-control project add \
  --id my-app \
  --path /home/codex/projects/my-app \
  --host lightsail-main \
  --adapter generic_git
```

### Project OS 프로젝트

먼저 대상 프로젝트 자체에 Project OS scaffold와 `.project-os/manifest.yaml`이 준비되어 있어야 합니다.

```bash
remote-control project add \
  --id dailytown \
  --name DailyTown \
  --path /home/codex/projects/dailytown \
  --host lightsail-main \
  --adapter project-os \
  --role developer
```

이 명령이 수정하는 것은 Remote Control의 `config/projects.yaml`뿐입니다. `.project-os` 내부 상태는 직접 수정하지 않습니다.

여러 Host를 쓸 경우 각 Host의 checkout 경로를 `config/projects.yaml`에 추가합니다.

```yaml
projects:
  dailytown:
    name: DailyTown
    adapter: project_os
    adapter_config:
      role: developer
      actor: remote-control-codex
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

## Telegram 사용

```text
/projects
/hosts
/run dailytown
/run dailytown --host desktop-main
/status
/jobs
/job JOB-...
```

일시정지/재개:

```text
/pause
/resume
```

추가 지시:

```text
/steer UI는 건드리지 말고 backend만 수정해
```

중지:

```text
/stop
/stop JOB-...
```

## R5 Project OS 실행 흐름

Project OS adapter는 Controller가 Task 내용을 추측하지 않고 실제 `projectctl` 계약을 사용합니다.

```text
/run dailytown
   ↓
projectctl status --json
   ↓
projectctl next --role developer --json
   ↓
ProjectWork에 Task/Host binding 저장
   ↓
projectctl context TASK-... --role developer
   ↓
projectctl claim TASK-... --role developer
   ↓
Codex 실행
   ↓
WAITING_AGENT
   ↓
projectctl submit TASK-... <result.yaml> --role ... --actor ...
   ↓
Remote Job COMPLETED
```

중요한 의미 차이:

- Remote Job의 `COMPLETED`는 **구현 turn과 implementation handoff 제출 완료**를 뜻합니다.
- Project OS Task가 `done`이 되었다는 뜻은 아닙니다.
- Task 완료/PASS는 Project OS의 독립 review/evaluation 흐름이 결정합니다.
- Remote Control은 Project OS backlog/current YAML을 직접 수정하지 않습니다.

Task를 claim한 뒤에는 해당 ProjectWork가 **원래 Host에 pin**됩니다. recovery 중 `auto` routing이 켜져 있어도 다른 checkout으로 Task를 넘기지 않습니다.

Controller가 claim 직후 재시작해도 Project OS의 `current_tasks`를 확인해 중복 claim을 피합니다. 결과 제출 중 재시작하면 Codex를 다시 돌리지 않고 `FINALIZE` recovery로 submit 단계만 재시도합니다.

## Generic Git Adapter

Project OS가 없는 프로젝트는 계속 독립적으로 사용할 수 있습니다.

Generic Git adapter가 실행 전에 수집하는 정보:

- current branch
- `git status --short --branch`
- `git diff --stat`

그 후 Codex가 repository-local instructions와 filesystem 상태를 기준으로 작업합니다.

## 장애 복구

R4 기능은 그대로 유지됩니다.

- Host offline → `WAITING_HOST`
- heartbeat expiry → persisted OFFLINE
- Codex quota → `WAITING_QUOTA`
- 기본 quota backoff: 30분 → 1시간 → 2시간 cap
- Controller restart reconciliation
- 살아 있는 Desktop execution adopt
- Human Gate expiry fail-closed

R5에서는 Project OS 결과 제출 단계도 recovery 대상입니다.

## Desktop Runner

`.env` 예시:

```dotenv
REMOTE_RUNNER_CONTROLLER_WS=wss://YOUR-CONTROLLER/ws/runner
REMOTE_RUNNER_TOKEN=<controller와 동일한 transport secret>
REMOTE_RUNNER_HOST_ID=desktop-main
REMOTE_RUNNER_NAME=Main Desktop
REMOTE_RUNNER_OS=windows
REMOTE_RUNNER_CAPABILITIES=codex,git,projectctl,android,gui,browser

CODEX_EXECUTABLE=codex
PROJECTCTL_EXECUTABLE=projectctl
GIT_EXECUTABLE=git
```

실행:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

Desktop Runner는 Controller로 outbound WebSocket만 연결합니다. Project OS 명령도 Controller가 raw shell을 보내는 방식이 아니라 정해진 Project Operation RPC만 실행합니다.

## Runtime API

```text
GET /health
GET /projects
GET /hosts
GET /jobs
GET /sessions
GET /approvals
GET /recovery
GET /project-work
GET /jobs/{job_id}/project-work
```

HTTP API는 기본적으로 `127.0.0.1`에 bind합니다.

## Security 핵심

- Telegram 사용자 allowlist
- Messenger text → shell 변환 금지
- Codex/process 실행은 direct argv
- Project working directory는 server-side registry에서만 결정
- Project Operation은 whitelist만 허용
- task/role/actor 식별자 validation
- Codex auth는 각 실행 Host 로컬에만 존재
- Desktop Runner는 outbound-only
- Project OS canonical YAML은 `projectctl`만 변경

## 상세 문서

- [Architecture](docs/ARCHITECTURE.md)
- [Project Adapters](docs/PROJECT_ADAPTERS.md)
- [Recovery & Scheduler](docs/RECOVERY.md)
- [Sessions and Feedback](docs/SESSIONS_AND_FEEDBACK.md)
- [Human Gate](docs/HUMAN_GATE.md)
- [Messaging](docs/MESSAGING.md)
- [Runners](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)
- [Security](docs/SECURITY.md)
- [R4 Manual Smoke Test](docs/SMOKE_TEST_R4.md)
- [R5 Manual Smoke Test](docs/SMOKE_TEST_R5.md)

## Roadmap

- [x] R0 — Telegram + Lightsail Codex
- [x] R1 — Desktop Runner / WebSocket / heartbeat
- [x] R2 — Session resume / steering / progress feedback
- [x] R3 — Human Gate
- [x] R4 — retry / quota / restart recovery
- [x] R5 — Project OS adapter
- [ ] R6 — Slack / Web UI

Package version: **0.6.0**
