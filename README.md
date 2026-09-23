# Remote Agent Control

Telegram/Slack에서 **현재 머신의 Codex**를 실행하고, 진행 상태·추가 지시·Human Gate·사용량 제한 복구·Project OS 연동까지 관리하는 Runtime Control Plane입니다.

현재 **R0 ~ R6 + Telegram Project Topics + production hardening**이 구현되어 있으며 package version은 **0.8.0**입니다.

## 전체 개요

기본 사용 방식은 **각 실행 머신이 독립적인 Controller 노드**가 되는 것입니다.

```text
Telegram Bot - Desktop
        ↓
Desktop Controller
        ↓
Desktop Codex CLI
        ↓
Desktop local projects


Telegram Bot - Lightsail
        ↓
Lightsail Controller
        ↓
Lightsail Codex CLI
        ↓
Lightsail local projects
```

즉 기본 구성에서:

- Lightsail이 Desktop을 제어하지 않습니다.
- Desktop이 Lightsail을 제어하지 않습니다.
- 각 노드는 자기 `.env`, `remote-control.db`, `config/projects.yaml`, Codex login을 가집니다.
- 사용자는 실행하고 싶은 머신의 Messenger bot으로 명령을 보냅니다.

Remote Agent Control과 Project OS의 역할도 분리됩니다.

- **Project OS**: 무엇을 해야 하는지 관리
  - Task
  - Context
  - Decision
  - implementation handoff
- **Remote Agent Control**: 현재 머신에서 어떻게 실행하는지 관리
  - Job
  - Host
  - Codex Session
  - Human Gate
  - Recovery
  - ProjectWork binding
- **Codex**: 실제 코드 수정과 구현 수행

지원 기능:

- Telegram polling
- Slack Socket Mode
- Windows/Desktop local Codex
- Lightsail/Linux local Codex
- Codex session resume / steering
- Human Gate
- Codex usage/quota 자동 재시도
- Controller restart recovery
- Generic Git 프로젝트
- Project OS 프로젝트
- read-only Web Dashboard

> Advanced: Controller와 다른 머신을 WebSocket Runner로 연결하는 remote-runner 기능도 유지됩니다. 하지만 **Desktop과 Lightsail을 각각 Messenger로 독립 제어**하려는 경우에는 필요하지 않습니다.

---

# Quick Guide

## 1. 공통 — Telegram Bot 준비

각 독립 Controller에는 **별도 Telegram Bot 사용을 권장**합니다.

예:

```text
@my_desktop_codex_bot   → Desktop Controller
@my_lightsail_codex_bot → Lightsail Controller
```

두 Controller가 같은 Bot token으로 동시에 long polling하면 같은 update stream을 경쟁해서 소비할 수 있으므로 독립 노드 구성에서는 Bot을 분리하는 편이 안전합니다.

Telegram의 `@BotFather`에서:

```text
/newbot
```

으로 Bot을 만든 후 token을 보관합니다.

본인의 numeric Telegram User ID도 확인합니다. Controller를 시작하기 전에 Bot에 메시지를 한 번 보낸 뒤:

```text
https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```

응답의 다음 값을 사용합니다.

```json
{
  "message": {
    "from": {
      "id": 123456789
    }
  }
}
```

이 값이:

```dotenv
TELEGRAM_ALLOWED_USER_IDS=123456789
```

에 들어갑니다.

### Telegram Project Topics 활성화

프로젝트별 대화창을 사용하려면 BotFather에서 해당 Bot의 **private chat Topics** 기능을 활성화합니다.

Remote Control은 Controller 시작 시 Telegram command menu를 자동 등록합니다.

```text
/start
/help
/projects
/sync
/run
/status
/jobs
/job
/pause
/resume
/steer
/stop
```

Bot과 대화를 시작한 뒤:

```text
/start
```

또는:

```text
/sync
```

를 보내면 `config/projects.yaml`의 프로젝트별 Topic을 생성하거나 이름을 동기화합니다.

예:

```text
Desktop Codex Bot
├─ Nothing Wrong
├─ The Orpheus Project
└─ Tab Pets
```

Topic mapping과 Job message binding은 `remote-control.db`에 저장되므로 Controller 재시작 후에도 유지됩니다.

> Private Topic을 사용하려면 `python-telegram-bot>=22.6`이 필요합니다. `pip install -e ".[dev]"`로 최신 dependency를 반영하세요.

---

## 2. Desktop 최초 설정 — Windows

### 2-1. 설치

PowerShell:

```powershell
git clone https://github.com/Seunghyun0606/remote-control-pjt.git
cd remote-control-pjt

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
Copy-Item .env.example .env
```

확인:

```powershell
python --version
git --version
codex --version
```

Codex 로그인이 안 되어 있다면:

```powershell
codex
```

를 실행하고 Codex가 안내하는 ChatGPT 로그인 절차를 완료합니다.

Remote Control은 Codex credential을 저장하지 않고 **현재 Windows 사용자에 로그인된 Codex CLI**를 사용합니다.

### 2-2. Desktop `.env`

먼저 secret 생성:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

`.env`의 주요 값:

```dotenv
REMOTE_CONTROL_HOST_ID=desktop-main

REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_API_HOST=127.0.0.1
REMOTE_CONTROL_API_PORT=8787

TELEGRAM_BOT_TOKEN=<DESKTOP_BOT_TOKEN>
TELEGRAM_ALLOWED_USER_IDS=<YOUR_NUMERIC_TELEGRAM_USER_ID>

CONTROLLER_RUNNER_TOKEN=<GENERATED_RANDOM_SECRET>

CODEX_EXECUTABLE=codex
GIT_EXECUTABLE=git

REMOTE_CONTROL_SLACK_ENABLED=false
REMOTE_CONTROL_WEB_UI_ENABLED=true
```

로컬 Desktop-only 사용에서는 `REMOTE_RUNNER_*` 설정을 하지 않아도 됩니다.

### 2-3. Desktop 프로젝트 등록

일반 Git 프로젝트:

```powershell
remote-control project add --id my-project --name "My Project" --path "C:\dev\my-project" --host desktop-main --adapter generic_git
```

Project OS 프로젝트:

```powershell
remote-control project add --id my-project --name "My Project" --path "C:\dev\my-project" --host desktop-main --adapter project-os --role developer
```

등록 결과 확인:

```powershell
Get-Content .\config\projects.yaml
```

Controller는 시작할 때 Project Registry를 읽으므로 프로젝트를 추가했다면 Controller를 재시작합니다.

### 2-4. Desktop Controller 시작

```powershell
remote-control controller start
```

Telegram의 Desktop Bot에서 먼저:

```text
/start
/sync
/projects
/hosts
/status
```

`/sync` 후 생성된 각 Project Topic에서 명령과 일반 대화를 사용할 수 있습니다.

`/hosts`에서 다음과 비슷하게 보여야 합니다.

```text
desktop-main ONLINE
```

실행은 두 방식 모두 가능합니다.

Bot 기본 대화:

```text
/run my-project
```

해당 Project Topic:

```text
/run
```

Project Topic에서 일반 문장을 보내면 active Job이 없을 때는 그 문장으로 새 Codex Job을 시작하고, active Job이 하나면 같은 Job의 추가 지시로 처리합니다.

이 경로로 실행됩니다.

```text
Desktop Telegram Bot
        ↓
Desktop Controller
        ↓
Desktop Codex
        ↓
C:\dev\my-project
```

---

## 3. Lightsail 최초 설정 — Linux

### 3-1. 설치

Lightsail SSH:

```bash
git clone https://github.com/Seunghyun0606/remote-control-pjt.git
cd remote-control-pjt

python3.11 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
```

확인:

```bash
python --version
git --version
codex --version
```

Codex 로그인이 필요하면:

```bash
codex
```

를 실행하고 안내되는 로그인 절차를 완료합니다.

### 3-2. Lightsail `.env`

secret 생성:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

설정:

```dotenv
REMOTE_CONTROL_HOST_ID=lightsail-main

REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_API_HOST=127.0.0.1
REMOTE_CONTROL_API_PORT=8787

TELEGRAM_BOT_TOKEN=<LIGHTSAIL_BOT_TOKEN>
TELEGRAM_ALLOWED_USER_IDS=<YOUR_NUMERIC_TELEGRAM_USER_ID>

CONTROLLER_RUNNER_TOKEN=<GENERATED_RANDOM_SECRET>

CODEX_EXECUTABLE=codex
GIT_EXECUTABLE=git

REMOTE_CONTROL_SLACK_ENABLED=false
REMOTE_CONTROL_WEB_UI_ENABLED=true
```

Desktop Bot과는 다른 Bot token 사용을 권장합니다.

### 3-3. Lightsail 프로젝트 등록

예:

```bash
remote-control project add \
  --id my-project \
  --name "My Project" \
  --path /home/ubuntu/projects/my-project \
  --host lightsail-main \
  --adapter generic_git
```

Project OS:

```bash
remote-control project add \
  --id my-project \
  --name "My Project" \
  --path /home/ubuntu/projects/my-project \
  --host lightsail-main \
  --adapter project-os \
  --role developer
```

### 3-4. Lightsail Controller 시작

```bash
remote-control controller start
```

Lightsail Bot에서:

```text
/start
/sync
/projects
/hosts
/status
```

Bot 기본 대화에서는:

```text
/run my-project
```

Project Topic에서는:

```text
/run
```

만으로 해당 프로젝트의 Codex를 시작할 수 있습니다.

이 경로로 실행됩니다.

```text
Lightsail Telegram Bot
        ↓
Lightsail Controller
        ↓
Lightsail Codex
        ↓
/home/ubuntu/projects/my-project
```

---

## 4. 최초 동작 확인

각 노드에서 아래 순서만 확인하면 됩니다.

```text
/projects
/hosts
/status
/run <project-id>
/jobs
/job <job-id>
```

추가 지시:

```text
/steer 현재 구현 상태부터 확인하고 기존 변경을 중복하지 마
```

일시정지/재개:

```text
/pause
/resume
```

중지:

```text
/stop
```

---

## 5. Codex 사용량 제한 자동 복구

Codex usage/quota가 소진되면 Job을 즉시 `FAILED` 처리하지 않습니다.

```text
RUNNING
   ↓ Codex usage limit
WAITING_QUOTA
   ↓ reset/retry time
RUNNING
   ↓
existing Codex session resume
   ↓
COMPLETED
```

인식 대상에는 다음이 포함됩니다.

- structured `rate_limit` / `usage_limit` event
- `usage_limit_exceeded`
- `rate_limit_exceeded`
- `You've hit your usage limit`
- `Usage limit reached`
- `Too many requests`

Codex가 structured reset timestamp를 주면 그 값을 우선 사용합니다.

텍스트에 다음처럼 reset 시간이 포함된 경우도 인식합니다.

```text
You've hit your usage limit ... try again at Sep 25th, 2026 2:20 PM.
```

텍스트에 timezone이 없으면 **Controller가 실행되는 Host의 local timezone**으로 해석합니다.

reset 시간을 알 수 없으면 기본 backoff:

```text
attempt 1: 30분
attempt 2: 60분
attempt 3+: 120분
```

설정:

```dotenv
REMOTE_CONTROL_SCHEDULER_INTERVAL_SECONDS=15
REMOTE_CONTROL_QUOTA_RETRY_INITIAL_SECONDS=1800
REMOTE_CONTROL_QUOTA_RETRY_MAX_SECONDS=7200
```

Telegram에서:

```text
/status
```

예:

```text
JOB-... my-project WAITING_QUOTA host=desktop-main recovery=QUOTA attempt=1 retry_at=...
```

상세:

```text
/job JOB-...
```

예:

```text
State: WAITING_QUOTA
Recovery: QUOTA
Recovery mode: RESUME
Retry attempt: 1
Next retry: ...
```

재시도 시간이 되면:

```text
↻ Codex quota 대기 시간이 끝나 자동 재시도합니다.
```

알림 후 기존 Codex session을 우선 resume합니다.

Controller를 재시작하더라도 recovery metadata는 SQLite에 저장되므로 `WAITING_QUOTA` Job을 다시 복구합니다.

---

# 세부 가이드

## 프로젝트 Registry

### Generic Git

```bash
remote-control project add \
  --id my-app \
  --path /absolute/path/to/my-app \
  --host <local-host-id> \
  --adapter generic_git
```

Generic Git adapter는 실행 전 다음을 수집합니다.

- current branch
- `git status --short --branch`
- `git diff --stat`

### Project OS

대상 repo에서 먼저:

```bash
projectctl status --json
projectctl next --role developer --json
```

등록:

```bash
remote-control project add \
  --id my-app \
  --path /absolute/path/to/my-app \
  --host <local-host-id> \
  --adapter project-os \
  --role developer
```

실행 흐름:

```text
/run my-app
   ↓
projectctl status --json
   ↓
projectctl next --role developer --json
   ↓
ProjectWork Task/Host binding
   ↓
projectctl context
   ↓
projectctl claim
   ↓
Codex
   ↓
projectctl submit
   ↓
Remote Job COMPLETED
```

**Remote Job `COMPLETED`는 Project OS Task가 PASS/done이라는 뜻이 아닙니다.**

Remote Control은 implementation handoff까지 담당하고 Project OS review/evaluation은 별도입니다.

---

## Messenger 명령

공통 Controller command:

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

Telegram 전용:

```text
/sync
```

Telegram Project Topic 안에서는 project id가 자동으로 scope됩니다.

```text
/run
/status
/jobs
/pause
/resume
/stop
```

예를 들어 `Nothing Wrong` Topic의 `/status`는 `nothing-wrong` Job만 표시합니다.

일반 메시지 처리:

```text
Project Topic + active Job 0개
→ 메시지 내용으로 새 Codex Job 시작

Project Topic + steer 가능한 Job 1개
→ 해당 Job에 추가 지시

Project Topic + steer 가능한 Job 2개 이상
→ Telegram Inline Button으로 Job 선택
```

Bot이 보낸 Job progress/result 메시지에 Telegram **Reply**로 답하면 그 메시지와 연결된 정확한 Job으로 지시가 전달됩니다.

```text
[Nothing Wrong / JOB-A]
⏳ UI 구현 중...

↳ Reply: UI는 유지하고 backend만 수정해
          ↓
        JOB-A steer
```

따라서 여러 프로젝트와 여러 Codex Job을 동시에 실행해도 Topic + Reply를 기준으로 대화를 분리할 수 있습니다.

---

## Session / Steering

기존 external Codex session이 있으면 우선:

```text
codex exec resume <session-id>
```

을 사용합니다.

resume이 불가능하면 repository/filesystem 상태를 다시 확인한 뒤 새 session으로 fallback합니다.

Codex session은 continuation 최적화이고, durable implementation state는 repository입니다.

상세:

- [Sessions and Feedback](docs/SESSIONS_AND_FEEDBACK.md)

---

## Human Gate

중요한 결정이 필요하면:

```text
WAITING_HUMAN
```

상태가 됩니다.

Messenger에서:

- option
- Details
- Reject

를 선택할 수 있습니다.

상세:

- [Human Gate](docs/HUMAN_GATE.md)

---

## Slack

Slack은 Socket Mode입니다.

```dotenv
REMOTE_CONTROL_SLACK_ENABLED=true
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U12345678
```

필요 설정:

- App-Level Token: `connections:write`
- Bot scopes:
  - `chat:write`
  - `im:history`
  - `im:write`
- Bot event:
  - `message.im`

상세:

- [Messaging](docs/MESSAGING.md)
- [R6 Manual Smoke Test](docs/SMOKE_TEST_R6.md)

---

## Web Dashboard

```text
GET /dashboard
GET /ui
```

`/ui`는 read-only이며 기본적으로 5초마다 runtime snapshot을 갱신합니다.

설정:

```dotenv
REMOTE_CONTROL_WEB_UI_ENABLED=true
```

Controller는 기본적으로:

```text
127.0.0.1:8787
```

에 bind합니다.

외부 공개가 필요하면 인증 reverse proxy/private network를 사용하세요.

상세:

- [Web Dashboard](docs/WEB_UI.md)

---

## Advanced — Remote Runner

다음 구성도 지원합니다.

```text
Messenger
   ↓
Controller Host
   ↓ WebSocket
Remote Desktop Runner
   ↓
Codex
```

하지만 **Desktop과 Lightsail 각각에서 Messenger를 통해 그 머신의 Codex를 실행**하는 것이 목적이라면 사용하지 않아도 됩니다.

Remote Runner가 필요한 경우에만:

```dotenv
REMOTE_RUNNER_CONTROLLER_WS=wss://YOUR-CONTROLLER/ws/runner
REMOTE_RUNNER_TOKEN=<controller token>
REMOTE_RUNNER_HOST_ID=desktop-main
```

을 설정하고:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

합니다.

상세:

- [Runners](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)

---

## Security 핵심

- Telegram / Slack 사용자 allowlist
- Messenger text → arbitrary shell 변환 금지
- process 실행은 direct argv
- project path는 server-side registry에서만 결정
- Project Operation whitelist
- Codex auth는 실행 Host 로컬에만 존재
- Project OS canonical YAML은 `projectctl`만 변경
- Web UI는 read-only

---

## Runtime API

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
- [x] Production hardening — quota signal/retry visibility + independent local-node guide
- [x] Telegram Project Topics — command menu / topic scope / reply-to-job / multi-job selection

Package version: **0.8.0**
