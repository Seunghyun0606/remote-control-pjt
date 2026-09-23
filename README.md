# Remote Agent Control

Telegram/Slack에서 Lightsail 또는 Desktop의 Codex 작업을 시작하고, 진행 상태·추가 지시·Human Gate·장애 복구·Project OS 연동까지 원격으로 관리하는 Runtime Control Plane입니다.

현재 **R0 ~ R6**까지 구현되어 있으며 package version은 **0.7.0**입니다.

## 전체 개요

Remote Agent Control은 프로젝트 자체의 기획/Task 상태와 AI 실행 runtime을 분리합니다.

- **Project OS**: 무엇을 해야 하는지 관리
  - Task
  - Context
  - Decision
  - 구현 handoff
- **Remote Agent Control**: 어디서/어떻게 실행하는지 관리
  - Job
  - Host
  - Codex Session
  - Human Gate
  - Recovery
  - ProjectWork binding
- **Codex**: 실제 코드 수정과 구현 수행

전체 구조:

```text
Telegram / Slack
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

Browser → /ui read-only dashboard
```

지원 범위:

- Telegram 명령/알림
- Slack Socket Mode 명령/알림
- Lightsail 로컬 Codex 실행
- Windows/Desktop remote Runner
- Codex session resume / steering
- Human Gate
- Host/Quota/Restart recovery
- Generic Git 프로젝트
- Project OS 프로젝트
- read-only Web Dashboard

---

## 5분 Quick Guide

가장 먼저 **Messenger 없이 Controller와 Dashboard만** 확인하는 것을 권장합니다.

### 1. 설치

Python 3.11+가 필요합니다.

```bash
git clone https://github.com/Seunghyun0606/remote-control-pjt.git
cd remote-control-pjt

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

### 2. 자동 테스트

```bash
ruff check .
pytest -q
```

현재 main 기준 CI에서는 Python 3.11 / 3.12 모두 검증합니다.

### 3. Controller만 실행

Telegram 설정 없이 Controller/API/Dashboard부터 확인합니다.

```bash
remote-control controller start --no-telegram
```

기본 주소:

```text
http://127.0.0.1:8787
```

다른 터미널에서:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/dashboard
```

브라우저:

```text
http://127.0.0.1:8787/ui
```

정상이라면:

- `/health` → `{"status":"ok"}`
- `/dashboard` → runtime JSON
- `/ui` → read-only Dashboard

여기까지 통과하면 Controller 기본 설치는 정상입니다.

---

## 권장 테스트 순서

전체 시스템은 아래 순서로 검증하면 문제 구간을 쉽게 분리할 수 있습니다.

### 1단계 — Automated Test

```bash
ruff check .
pytest -q
```

검증 범위:

- Job/Host/Session state
- Human Gate
- Recovery
- Project OS Adapter
- Telegram/Slack notification routing
- Web Dashboard
- security boundary
- Runner protocol

### 2단계 — Controller / API / Web UI

```bash
remote-control controller start --no-telegram
```

확인:

```text
GET /health
GET /dashboard
GET /ui
GET /projects
GET /hosts
GET /jobs
```

이 단계에서는 Telegram/Slack/Codex 인증이 없어도 Controller runtime을 확인할 수 있습니다.

### 3단계 — Telegram

`.env`:

```dotenv
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_ALLOWED_USER_IDS=<numeric telegram user id>
```

실행:

```bash
remote-control controller start
```

Telegram에서:

```text
/projects
/hosts
/status
```

그 다음 실제 등록 프로젝트에서:

```text
/run <project-id>
/job JOB-...
/pause
/resume
/steer backend만 수정해
/stop
```

확인할 것:

- 허용된 사용자만 명령 가능
- Job id가 생성됨
- 진행/완료 알림이 Telegram으로 옴
- plain text가 shell command로 실행되지 않음
- Human Gate 발생 시 버튼으로 승인/거절 가능

### 4단계 — Slack

Slack App 준비:

- Socket Mode 활성화
- App-Level Token: `connections:write`
- Bot scopes:
  - `chat:write`
  - `im:history`
  - `im:write`
- Bot event:
  - `message.im`

`.env`:

```dotenv
REMOTE_CONTROL_SLACK_ENABLED=true
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U12345678
```

Controller 재시작 후 Slack DM에서:

```text
/hosts
/projects
/run <project-id>
```

확인할 것:

- Slack public webhook 포트 없이 연결됨
- allowlist 외 사용자는 거절됨
- Slack에서 시작한 Job의 후속 알림은 Slack으로만 감
- Telegram Job의 알림은 Telegram으로만 감
- Human Gate Option / Details / Reject 버튼이 동작함

상세 절차:

- [R6 Manual Smoke Test](docs/SMOKE_TEST_R6.md)
- [Messaging](docs/MESSAGING.md)

### 5단계 — Desktop Runner

Controller Host에서 `.env`:

```dotenv
CONTROLLER_RUNNER_TOKEN=<long-random-secret>
```

Windows/Desktop Runner:

```dotenv
REMOTE_RUNNER_CONTROLLER_WS=wss://YOUR-CONTROLLER/ws/runner
REMOTE_RUNNER_TOKEN=<controller와 동일한 secret>
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

Messenger에서:

```text
/hosts
```

`desktop-main`이 ONLINE인지 확인합니다.

그 다음:

```text
/run <project-id> --host desktop-main
```

확인할 것:

- Desktop은 Controller로 outbound WebSocket만 연결
- 지정한 Host에서 Codex가 실행
- Controller에 Codex credential이 복사되지 않음
- Job 결과가 원래 Messenger로 반환됨

### 6단계 — Project OS E2E

대상 프로젝트 자체에 Project OS scaffold가 있어야 합니다.

실행 Host의 대상 repo에서 먼저:

```bash
projectctl status --json
projectctl next --role developer --json
codex --version
```

프로젝트 등록:

```bash
remote-control project add \
  --id dailytown \
  --name DailyTown \
  --path /absolute/path/to/dailytown \
  --host lightsail-main \
  --adapter project-os \
  --role developer
```

Messenger:

```text
/run dailytown
```

정상 흐름:

```text
projectctl status
        ↓
projectctl next
        ↓
Task / Host binding
        ↓
projectctl context
        ↓
projectctl claim
        ↓
Codex
        ↓
WAITING_AGENT
        ↓
projectctl submit
        ↓
Remote Job COMPLETED
```

확인:

```text
/job JOB-...
GET /jobs/{job_id}/project-work
```

ProjectWork는 다음 형태여야 합니다.

```text
adapter=project_os
host_id=<execution-host>
task_id=TASK-...
status=SUBMITTED
```

중요:

**Remote Job COMPLETED는 Project OS Task가 PASS/done 되었다는 의미가 아닙니다.**

Remote Control은 구현 결과를 Project OS에 handoff하고, 최종 review/evaluation은 Project OS가 별도로 수행합니다.

상세 절차:

- [R5 Manual Smoke Test](docs/SMOKE_TEST_R5.md)
- [Project Adapters](docs/PROJECT_ADAPTERS.md)

---

## 세부 가이드

### 환경 설정

주요 Controller 설정:

```dotenv
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_HOST_ID=lightsail-main

REMOTE_CONTROL_API_HOST=127.0.0.1
REMOTE_CONTROL_API_PORT=8787

CONTROLLER_RUNNER_TOKEN=<long-random-secret>

CODEX_EXECUTABLE=codex
CODEX_SANDBOX=workspace-write
CODEX_APPROVAL_POLICY=never

PROJECTCTL_EXECUTABLE=projectctl
GIT_EXECUTABLE=git
```

전체 예시는 [`.env.example`](.env.example)을 참고합니다.

### 프로젝트 등록

#### 일반 Git 프로젝트

```bash
remote-control project add \
  --id my-app \
  --path /home/codex/projects/my-app \
  --host lightsail-main \
  --adapter generic_git
```

Generic Git adapter는 실행 전에 다음 정보를 읽습니다.

- current branch
- `git status --short --branch`
- `git diff --stat`

Project OS가 없어도 동작합니다.

#### Project OS 프로젝트

```bash
remote-control project add \
  --id dailytown \
  --name DailyTown \
  --path /home/codex/projects/dailytown \
  --host lightsail-main \
  --adapter project-os \
  --role developer
```

Remote Control이 수정하는 것은 자신의 `config/projects.yaml` registry입니다.

Project OS canonical state는 직접 수정하지 않고 항상 `projectctl`을 통해 접근합니다.

여러 Host를 사용할 경우:

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

### Messenger 명령

Telegram과 Slack DM에서 동일한 Controller command를 사용합니다.

```text
/projects
/hosts
/status
/jobs
/job <job-id>

/run <project-id>
/run <project-id> --host <host-id>

/pause [job-id]
/resume [job-id]

/steer [--job <job-id>] <instruction>
/send [--job <job-id>] <instruction>

/stop [job-id]
```

Job이 하나뿐이고 Human Gate가 없다면 일반 text도 해당 Job의 steering으로 처리할 수 있습니다.

### Session / Steering

Codex session이 존재하면:

```text
codex exec resume
```

형태로 continuation을 시도합니다.

Session resume이 불가능하면 repository/filesystem 상태를 다시 읽어 새 Codex session으로 fallback합니다.

따라서 Codex session 자체는 durable source of truth가 아닙니다.

상세:

- [Sessions and Feedback](docs/SESSIONS_AND_FEEDBACK.md)

### Human Gate

중요 결정이 필요한 경우 Job은:

```text
WAITING_HUMAN
```

으로 전환됩니다.

Messenger에서는:

- option 선택
- Details
- Reject

를 사용할 수 있습니다.

Approval 결과는 shell command가 아니라 기존 Codex session continuation instruction으로 전달됩니다.

상세:

- [Human Gate](docs/HUMAN_GATE.md)

### Slack

Slack은 Socket Mode를 사용합니다.

```dotenv
REMOTE_CONTROL_SLACK_ENABLED=true
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U12345678,U87654321
```

Slack MVP는 DM-first입니다.

Telegram과 Slack을 동시에 활성화해도 notification은 Job을 생성한 channel로 돌아갑니다.

상세:

- [Messaging](docs/MESSAGING.md)
- [R6 Manual Smoke Test](docs/SMOKE_TEST_R6.md)

### Web Dashboard

```text
GET /dashboard
GET /ui
```

`/ui`는 5초마다 runtime snapshot을 갱신하는 read-only 화면입니다.

표시 정보:

- Project 수
- Host 상태
- active / queued / failed Job
- Project OS Task binding
- quota wait
- Human Gate
- recovery 상태

의도적으로 다음 control은 제공하지 않습니다.

- run
- steer
- stop
- approval
- shell execution

설정:

```dotenv
REMOTE_CONTROL_WEB_UI_ENABLED=true
```

상세:

- [Web Dashboard](docs/WEB_UI.md)

### 장애 복구

지원하는 주요 recovery:

- Host offline → `WAITING_HOST`
- heartbeat expiry → OFFLINE
- Codex quota → `WAITING_QUOTA`
- quota backoff
  - 30분
  - 1시간
  - 2시간 cap
- Controller restart reconciliation
- Desktop execution adoption
- Human Gate expiry fail-closed
- Project OS finalization retry

Project OS Task를 claim한 뒤에는 ProjectWork가 원래 Host에 pin됩니다.

Recovery 중 다른 checkout으로 자동 이동하지 않습니다.

상세:

- [Recovery & Scheduler](docs/RECOVERY.md)

### Runtime API

주요 조회 API:

```text
GET /health
GET /dashboard
GET /ui
GET /projects
GET /hosts
GET /jobs
GET /sessions
GET /approvals
GET /recovery
GET /project-work
GET /jobs/{job_id}/project-work
```

Controller HTTP server는 기본적으로:

```text
127.0.0.1:8787
```

에 bind합니다.

외부에서 Dashboard/API를 접근해야 한다면 public exposure보다 private network 또는 인증된 reverse proxy를 권장합니다.

### Security 핵심

- Telegram / Slack 사용자 allowlist
- Messenger text → shell 변환 금지
- direct subprocess argv 사용
- project path는 server-side registry에서만 결정
- Project Operation whitelist
- task / role / actor validation
- Codex auth는 실행 Host 로컬에만 존재
- Desktop Runner outbound-only
- Slack Socket Mode outbound connection
- Project OS canonical YAML은 `projectctl`만 변경
- Web UI는 read-only

---

## 상세 문서

- [Architecture](docs/ARCHITECTURE.md)
- [Project Adapters](docs/PROJECT_ADAPTERS.md)
- [Recovery & Scheduler](docs/RECOVERY.md)
- [Sessions and Feedback](docs/SESSIONS_AND_FEEDBACK.md)
- [Human Gate](docs/HUMAN_GATE.md)
- [Messaging](docs/MESSAGING.md)
- [Web Dashboard](docs/WEB_UI.md)
- [Runners](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)
- [Security](docs/SECURITY.md)

Manual smoke test:

- [R4 Manual Smoke Test](docs/SMOKE_TEST_R4.md)
- [R5 Manual Smoke Test](docs/SMOKE_TEST_R5.md)
- [R6 Manual Smoke Test](docs/SMOKE_TEST_R6.md)

---

## Roadmap

- [x] R0 — Telegram + Lightsail Codex
- [x] R1 — Desktop Runner / WebSocket / heartbeat
- [x] R2 — Session resume / steering / progress feedback
- [x] R3 — Human Gate
- [x] R4 — retry / quota / restart recovery
- [x] R5 — Project OS adapter
- [x] R6 — Slack / Web UI

Package version: **0.7.0**
