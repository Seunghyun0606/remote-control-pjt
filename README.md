# Remote Agent Control

Telegram/Slack에서 **현재 머신의 Codex**를 실행하고, 진행 상태·추가 지시·Human Gate·사용량 제한 복구·Project OS 연동까지 관리하는 Runtime Control Plane입니다.

현재 **R0 ~ R6 + Telegram Project Topics + production hardening**이 구현되어 있으며 package version은 **0.11.1**입니다.

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

## Persistent Project Session

v0.10부터 Remote Control이 Project별 Codex context의 소유자가 됩니다. Project OS에는 Codex session metadata를 저장하지 않습니다.

```text
Telegram Project Topic / API
            ↓
Remote Control Project Session
            ↓
Codex thread
            ↓
Job 1 → Job 2 → Job 3
```

같은 사용자와 같은 Project에서 Job이 완료된 뒤 다음 일반 메시지나 `/run`으로 새 Job을 만들면, 기존 Project Session의 `external_session_id`를 새 Job에 연결하고 `codex exec resume <thread-id>`를 사용합니다. Project Session은 사용자별 context를 관리하고, 실제 repository 동시 실행은 별도의 **working-tree execution lease**가 제어합니다. 따라서 Telegram/Slack/API 사용자 ID가 달라도 같은 Host의 같은 working directory에는 동시에 두 개의 write-capable Codex Job이 실행되지 않습니다.

Project Topic에서 사용할 수 있는 명령:

```text
/session                         현재 active Project Session
/session new                     새 Project Session 생성
/session use <project-session-id> 과거 Session을 다시 active로 전환
/sessions                        최근 Project Session 목록
```

Session rollover는 이전 Codex thread를 삭제하지 않습니다. Remote Control DB에서는 CLOSED history로 남기고 다음 Job이 새 thread를 생성합니다.

과거 Session으로 돌아가려면 `/sessions`에서 ID를 찾은 뒤 `/session use <project-session-id>`를 실행합니다. 현재 active Session은 CLOSED history로 전환되고 선택한 과거 Session이 다시 IDLE/active 대상이 됩니다. 다음 새 Job은 그 Session의 Codex thread를 resume합니다.


Desktop에서 Telegram이 사용한 Codex thread를 직접 이어서 보려면 Remote Control과 interactive Codex가 같은 `CODEX_HOME`을 사용해야 합니다. `.env`에 예를 들어 다음처럼 지정합니다.

```dotenv
CODEX_HOME=C:/Users/<you>/.codex
```

그 후 `/session`에 표시되는 `Codex session` 값을 이용해 같은 머신에서 직접 resume할 수 있습니다.

```powershell
codex resume --include-non-interactive <codex-session-id>
```

Remote Runner를 사용하는 경우에도 해당 Runner 프로세스의 `CODEX_HOME`이 실제 Desktop Codex store와 같아야 합니다. Session은 host-local Codex storage에 의존하므로 기존 thread가 있는 Project는 가능한 한 같은 host에서 이어가는 것이 안전합니다.

Resume 시 Codex가 요청한 thread와 다른 `thread.started` ID를 반환하면 Remote Control은 정상 resume으로 인정하지 않습니다. 명시적인 session/thread 부재 또는 `SESSION_IDENTITY_MISMATCH`인 경우에만 repository state를 다시 읽는 새 Codex thread로 복구합니다. CLI 옵션 오류, 권한/파일 오류, 실행 실패, 알 수 없는 Codex 오류는 새 thread로 숨기지 않고 원래 Job 실패로 남깁니다.

### WAITING Job 수동 재실행과 재시작 복구

`/retry <job-id>`는 상태에 따라 다르게 동작합니다.

- `FAILED`: 기존 동작대로 새 retry Job을 생성합니다.
- `WAITING_HOST`: 같은 Job을 즉시 다시 실행 시도합니다.
- `WAITING_QUOTA`: 예약된 retry 시각을 기다리지 않고 같은 Job을 즉시 resume 시도합니다.
- `WAITING_HUMAN`: Human Gate 결정을 우회하지 않으며, 승인/거절 응답이 필요합니다.

Controller가 재시작될 때 이미 `WAITING_HOST`였던 Job은 즉시 recovery due 상태로 다시 등록됩니다. RecoveryScheduler는 시작 직후 첫 tick을 동기적으로 실행하므로 사용 가능한 host라면 별도의 다음 scheduler interval을 기다리지 않고 재개를 시도합니다. `WAITING_QUOTA`의 미래 retry 시각은 재시작만으로 무시하지 않으며, 즉시 시도하려면 사용자가 `/retry <job-id>`를 실행합니다.

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
/retry
/sessions
/session
/doctor
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

pip install -c constraints/dev.txt -e ".[dev]"
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

# Use an absolute path so Job/Session history does not depend on the launch directory.
REMOTE_CONTROL_HOME=C:/path/to/remote-control-pjt
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_API_HOST=127.0.0.1
REMOTE_CONTROL_API_PORT=8787

TELEGRAM_BOT_TOKEN=<DESKTOP_BOT_TOKEN>
TELEGRAM_ALLOWED_USER_IDS=<YOUR_NUMERIC_TELEGRAM_USER_ID>

CONTROLLER_RUNNER_TOKEN=<GENERATED_RANDOM_SECRET>
# 127.0.0.1 외 주소에 bind할 때 필수
CONTROLLER_API_TOKEN=

CODEX_EXECUTABLE=codex
# Desktop Codex와 같은 thread store를 공유하려면 동일한 home을 사용합니다.
CODEX_HOME=C:/Users/<you>/.codex
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

### 2-4. Desktop 실행환경 진단

Controller를 시작하기 전에:

```powershell
remote-control doctor
```

를 실행합니다. Windows npm 설치의 `codex.cmd` / `codex.ps1`도 자동으로 resolve하며, Codex/Git/프로젝트 경로와 실제 DB 위치를 표시합니다.

예:

```text
Remote Control Doctor
✅ Host: desktop-main
✅ Codex: C:\Users\...\npm\codex.CMD (cmd); codex-cli ...
✅ Git: C:\Program Files\Git\cmd\git.EXE ...
✅ Project tab-pets: C:\...\tab-pets
✅ Runtime Home: C:\...\remote-control-pjt
```

`REMOTE_CONTROL_HOME`을 절대경로로 지정하면 DB/config 경로를 고정할 수 있습니다.

Controller를 repo 밖이나 Windows Task Scheduler/서비스에서 시작할 때는 `.env` 자체도 절대경로로 지정하세요.

```powershell
remote-control doctor --env-file "C:\path\to\remote-control-pjt\.env"

remote-control controller start --env-file "C:\path\to\remote-control-pjt\.env"
```

이렇게 하면 **현재 PowerShell 작업 디렉터리와 무관하게 동일한 `.env` + `REMOTE_CONTROL_HOME` + DB/config**를 사용합니다.

### 2-5. Desktop Controller 시작

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

REMOTE_CONTROL_HOME=/home/ubuntu/remote-control-pjt
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

### 3-4. Lightsail 실행환경 진단

Controller를 시작하기 전에:

```bash
remote-control doctor
```

를 실행해 Codex/Git, 프로젝트 경로, 실제 DB/config 경로를 확인합니다.

systemd 등에서 repo 밖의 작업 디렉터리로 시작할 예정이라면:

```bash
remote-control doctor --env-file /home/ubuntu/remote-control-pjt/.env
```

처럼 `.env`도 절대경로로 확인하는 편이 안전합니다.

### 3-5. Lightsail Controller 시작

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

각 노드에서 아래 순서로 확인하면 됩니다.

CLI에서 먼저:

```text
remote-control doctor
```

Telegram에서:

```text
/start
/sync
/projects
/hosts
/status
/run <project-id>
/jobs
/job <job-id>
/sessions
/doctor
```

Project Topic 안에서는 `/run <project-id>` 대신 `/run`만 사용할 수 있습니다.

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

## 5. Job 상태와 복구 방식

상태에 따라 사용자가 해야 할 동작이 다릅니다.

| State | 의미 | 자동 복구 | 사용자가 할 일 |
|---|---|---|---|
| `WAITING_HOST` | 실행 가능한 Host를 기다리는 중 | O | Host/Controller 설정을 정상화하고 대기 |
| `WAITING_QUOTA` | Codex usage/quota reset 대기 | O | 보통 대기만 하면 됨 |
| `WAITING_HUMAN` | Human Gate 응답 대기 | X | Telegram 버튼/선택지로 결정 |
| `PAUSED` | 사용자가 일시정지 | X | `/resume` |
| `CANCELLING` | 중지 요청 후 실제 Codex 종료 확인 대기 | Runner reconnect 시 cancel 재확인 | 새 Job을 시작하지 말고 종료 확인 대기 |
| `FAILED` | 복구 불가 오류로 종료 | X | 원인 수정 후 `/retry <job-id>` 또는 새 `/run` |
| `COMPLETED` | 정상 종료 | X | 필요하면 새 Job 시작 |
| `CANCELLED` | 중지됨 | X | 필요하면 새 Job 시작 |

특히:

```text
/resume
```

은 `PAUSED` Job 전용입니다. `FAILED` Job은 자동으로 재실행되지 않으며 `/retry`를 사용해야 합니다.

`WAITING_HOST`와 `WAITING_QUOTA`는 recovery scheduler가 자동으로 다시 확인합니다.

`/stop`은 원격 Runner에 요청을 보낸 즉시 `CANCELLED`로 바꾸지 않습니다. 원격 Codex 프로세스의 terminal `JOB_RESULT`를 확인할 때까지 `CANCELLING`과 Project Session lock을 유지합니다. Runner 연결이 교체되는 경우에도 이전 WebSocket의 cleanup은 새 연결을 OFFLINE 처리하지 않습니다.

---

## 6. Codex 사용량 제한 자동 복구

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

# Troubleshooting

## `WAITING_HOST host=-`

예:

```text
JOB-... tab-pets WAITING_HOST host=- recovery=HOST ...
```

Codex가 멈춘 것이 아니라 **실행 가능한 Host를 아직 선택하지 못한 상태**입니다.

확인:

```text
/hosts
/status
```

Desktop이면 일반적으로 다음 세 값이 일치해야 합니다.

```text
.env
REMOTE_CONTROL_HOST_ID=desktop-main

projects.yaml
allowed_hosts:
  - desktop-main

projects.yaml
repository.path:
  desktop-main: C:\...\project
```

`/status`에는 v0.9부터 recovery reason도 표시됩니다. Host가 ONLINE이 되면 `WAITING_HOST` Job은 자동 재개됩니다.

## Windows에서 `[WinError 2]`

먼저:

```powershell
remote-control doctor
```

를 실행합니다.

v0.9부터 npm 설치의 `codex.cmd` / `codex.ps1` wrapper를 지원하므로 일반적인 Windows 설치에서는:

```dotenv
CODEX_EXECUTABLE=codex
```

를 그대로 사용할 수 있습니다.

`doctor`가 Codex/Git resolve 또는 version check에 실패하면 Controller 시작 전 해당 문제를 수정하세요.

## 재시작 후 기존 Job이 안 보임

기본 SQLite URL은 상대경로입니다.

```dotenv
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
```

실행 디렉터리가 달라지면 다른 DB를 연 것처럼 보일 수 있으므로 `REMOTE_CONTROL_HOME`을 **절대경로**로 지정하는 것을 권장합니다.

```dotenv
REMOTE_CONTROL_HOME=C:/absolute/path/to/remote-control-pjt
```

repo 밖, Task Scheduler, systemd 등에서 실행한다면 `.env`도 명시합니다.

```powershell
remote-control controller start --env-file "C:\absolute\path\to\remote-control-pjt\.env"
```

```bash
remote-control controller start --env-file /home/ubuntu/remote-control-pjt/.env
```

현재 실제 경로는:

```text
remote-control doctor
```

에서 확인할 수 있습니다.

## 프로젝트를 추가했는데 Telegram Topic이 없음

프로젝트를 `remote-control project add`로 추가한 뒤 Controller를 재시작하고 Telegram에서:

```text
/sync
```

를 실행합니다.

Controller는 시작 시 `config/projects.yaml`을 읽기 때문에 실행 중 registry가 자동 hot-reload되지는 않습니다.

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

Project OS를 사용하지 않는 프로젝트는 `generic_git`만으로 운영할 수 있습니다. Remote Control은 Job/Session/Recovery를 관리하지만 별도의 canonical Task queue는 만들지 않습니다.

장기 작업이라면 repo 안에 최소한 다음 정도의 문서를 두는 것을 권장합니다.

```text
README.md
AGENTS.md
docs/
  PLAN.md
  TODO.md
  DECISIONS.md
```

그러면 Project Topic에서 "다음 작업 진행해줘" 같은 일반 지시를 보낼 때 Codex가 repository 상태와 프로젝트 문서를 기준으로 다음 작업을 판단할 수 있습니다.

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
/retry <failed-job-id>

/sessions
/session <session-id>

/doctor

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


## 실패 Job 재시도

`FAILED`는 자동 복구 대상이 아닙니다. 환경 문제를 수정한 뒤:

```text
/retry JOB-...
```

를 사용합니다.

기존 FAILED Job의 이력은 그대로 유지하고 새 Job을 생성합니다.

```text
JOB-A FAILED
   ↓ /retry JOB-A
JOB-B RUNNING
```

기존 Codex session ID가 있으면 새 Job은 해당 session resume을 우선 시도합니다. session이 없으면 원래 instruction으로 새 Codex 실행을 시작합니다.

Telegram에서 `/job <job-id>` 또는 `/session <session-id>` 상세를 열었을 때 상태가 `FAILED`면 **Retry** 버튼, `PAUSED`면 **Resume** 버튼도 표시됩니다.

Project OS Job은 canonical task state 중복을 피하기 위해 `/retry`로 복제하지 않습니다. 이 경우 `/run`으로 현재 Project OS state를 다시 평가합니다.

---

## Codex Session 조회

Project Topic에서:

```text
/sessions
```

을 보내면 해당 사용자와 Project에 속한 최근 Codex Session만 표시합니다.

상세:

```text
/session SESSION-...
```

표시 항목:

- Remote Control Session ID
- Project / Job
- Job state
- Host
- Codex external session ID
- Session state
- Created / Last active
- Final result / Error

Remote Control DB에는 transcript 전체가 아니라 Job ↔ Codex session metadata를 저장합니다. 실제 Codex conversation은 Codex session 자체가 source of truth입니다.

---

## Runtime Doctor / Windows Codex

`remote-control doctor`와 Telegram `/doctor`는 현재 Host와 실행환경을 점검합니다.

Windows에서 npm으로 설치된 Codex가 다음처럼 보여도 정상입니다.

```text
codex.ps1
codex.cmd
```

v0.9.0부터 Remote Control이 `.cmd/.bat`은 `cmd.exe`, `.ps1`은 PowerShell launcher를 통해 실행하므로 native `codex.exe` 경로를 수동으로 찾아 `.env`에 넣을 필요가 없습니다.

Controller 시작 시에도 Codex/Git/필요한 projectctl executable을 preflight합니다. 실행 파일이 없으면 Job을 만든 뒤 모호한 `[WinError 2]`로 실패하는 대신 Controller 시작 단계에서 명확한 오류를 냅니다.

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

외부 bind가 필요하면 `CONTROLLER_API_TOKEN`을 반드시 설정해야 Controller가 시작됩니다. 토큰이 설정된 경우 `/health`를 제외한 REST/UI HTTP 요청은 Bearer 또는 `X-Remote-Control-Token` 인증이 필요합니다. 브라우저 공개는 여전히 인증 reverse proxy/private network를 권장합니다.

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
# 선택: 기본 ~/.remote-control/<host-id>-executions.json
REMOTE_RUNNER_STATE_PATH=
```

을 설정하고:

> Remote Control 0.11.0부터 Remote Runner는 **Protocol v2**를 사용합니다. Controller와 Runner를 반드시 같은 버전으로 함께 업데이트하세요. Runner는 실행 중/완료 execution을 durable journal에 기록하며, 재시작 시 이전 process 상태를 확인하기 전에는 새 실행을 받지 않습니다.


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
- non-loopback Control API bind는 `CONTROLLER_API_TOKEN` 필수
- Runner WebSocket secret과 Control API secret은 분리
- 같은 Host + working directory는 DB-backed execution lease로 단일 writer 보장
- Codex 종료 시 wrapper PID가 아니라 process tree 종료 확인
- Runner execution 결과는 Controller의 durable `JOB_RESULT_ACK` 전까지 journal에 유지
- Protocol v2로 구 Controller/Runner 혼용 차단

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
- [Cancellation and Runner Reconnect Safety](docs/CANCELLATION_AND_RECONNECT.md)
- [Runtime Hardening — P0/P1 Closure](docs/RUNTIME_HARDENING_P0_P1.md)
- [Runtime Hardening — P2](docs/RUNTIME_HARDENING_P2.md)
- [Startup Preflight — P1 Closure](docs/STARTUP_PREFLIGHT_P1.md)
- [Process Safety P0 Closure](docs/PROCESS_SAFETY_P0.md)
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
- [x] Runtime hardening v0.9 — Windows npm Codex wrappers / doctor / FAILED retry / session history / stable runtime home / Windows CI
- [x] Process safety v0.11 — Runner execution journal / process-tree containment / working-tree lease / durable result ACK / Protocol v2

Package version: **0.11.1**
