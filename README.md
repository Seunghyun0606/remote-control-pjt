# Remote Agent Control

Messenger에서 원격 머신의 AI coding agent를 실행하고, 실행 Host를 선택하고, 진행 상태를 받고, 같은 Codex session에 추가 지시와 Human Gate 결정을 전달하기 위한 Remote Agent Control Plane입니다.

Project OS와는 독립 프로젝트입니다. Project OS는 프로젝트의 장기 상태를 관리하고, Remote Agent Control은 Job/Host/Session/Approval 같은 runtime 상태와 원격 실행을 관리합니다.

현재 **R0 + R1 + R2 + R3**까지 구현되어 있습니다.

```text
Telegram
   ↓
Controller
   ├─ SQLite Job / Event / Host / Session / Approval State
   ├─ Lightsail Local Runner → Codex CLI
   └─ WebSocket Gateway
            ↑ outbound
       Desktop Runner → Codex CLI
```

R3에서 추가된 기능:

- persistent Approval Registry
- `RUNNING → WAITING_HUMAN → RUNNING` 상태 전이
- Human Gate event/marker 감지
- Telegram inline option / Details / Reject 버튼
- A/B 같은 텍스트 선택 응답
- Approval 응답 사용자 검증
- 응답 후 같은 Host/Codex session 우선 resume
- Desktop Runner `HUMAN_GATE` event 전달
- `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/respond`
- `HUMAN_GATE_CREATED` / `HUMAN_GATE_RESOLVED` Event Ledger

Quota/restart/host recovery와 Scheduler는 R4 이후입니다.

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

Controller의 주요 설정:

```dotenv
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_HOST_ID=lightsail-main
REMOTE_CONTROL_PROGRESS_INTERVAL_SECONDS=300

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789

CONTROLLER_RUNNER_TOKEN=<long-random-secret>
```

Controller 실행:

```bash
remote-control controller start
```

Telegram 없이 API만 테스트하려면:

```bash
remote-control controller start --no-telegram
```

Codex 인증은 Controller/Lightsail과 Desktop 각각의 실행 계정에서 직접 완료합니다. Codex 인증정보를 Controller DB로 복사하지 않습니다.

## Telegram에서 명령하는 방법

기본 실행:

```text
/projects
/hosts
/run dailytown
/run dailytown --host desktop-main
/status
/jobs
/job JOB-...
```

일시정지와 재개:

```text
/pause
/resume
```

실행 중 추가 지시:

```text
/steer UI는 건드리지 말고 backend만 수정해
```

또는 active Job이 하나뿐이면 일반 텍스트를 그대로 보낼 수 있습니다.

```text
UI는 건드리지 말고 backend만 수정해
```

R2 steering은 현재 Codex turn을 강제로 끊지 않습니다. 현재 turn이 끝난 뒤 같은 session을 resume하면서 추가 지시를 적용합니다.

Human Gate가 발생하면 Telegram에 다음 형태의 메시지가 옵니다.

```text
⚠ Human Gate

Save Schema v3 migration을 허용할까요?

A. 기존 Schema 유지
B. v3 Migration 진행

[A · 기존 Schema 유지] [B · v3 Migration 진행]
[Details] [Reject]
```

버튼을 누르거나 pending Approval이 하나뿐이면 다음처럼 선택 키를 직접 보낼 수도 있습니다.

```text
B
```

Controller는 Job을 `WAITING_HUMAN`에 두고 gated 작업을 진행하지 않습니다. 응답 후 기존 Host와 Codex session을 우선 사용해 새 turn으로 결정을 전달합니다.

중지:

```text
/stop
/stop JOB-...
```

`pause`는 session을 보존하고 사용자가 명시적으로 나중에 resume하려는 동작이고, `stop`은 Job을 `CANCELLED`로 끝내는 동작입니다.

## Desktop Runner 설치

Windows PowerShell:

```powershell
.\runner\windows\install.ps1
```

Desktop `.env`:

```dotenv
REMOTE_RUNNER_CONTROLLER_WS=wss://YOUR-CONTROLLER/ws/runner
REMOTE_RUNNER_TOKEN=<controller와 동일한 transport secret>
REMOTE_RUNNER_HOST_ID=desktop-main
REMOTE_RUNNER_NAME=Main Desktop
REMOTE_RUNNER_OS=windows
REMOTE_RUNNER_CAPABILITIES=codex,git,android,gui,browser
```

Codex CLI 인증 후:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

Desktop은 Controller로 outbound WebSocket만 생성합니다. Desktop inbound port는 열지 않습니다.

## Lightsail Runner 설치

Controller Host는 기본적으로 local Codex Runner를 사용합니다.

```bash
codex --version
remote-control controller start
```

Lightsail 계정의 Codex 인증은 Lightsail에만 존재합니다.

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

Messenger에서 임의 filesystem path를 전달할 수 없습니다. Controller의 Project Registry에 등록된 경로만 사용합니다.

## 명령 목록

- `/projects`: 등록 프로젝트
- `/hosts`: Host online/offline 상태
- `/status`: 현재 사용자의 active Job. Human Gate 대기 시 `WAITING_HUMAN` 표시
- `/run <project> [--host <id>]`: 새 Job 시작
- `/jobs`: 최근 Job
- `/job <id>`: Job/외부 Codex session 확인
- `/pause [job-id]`: 실행을 일시정지하고 session 보존
- `/resume [job-id]`: session resume, 실패 시 repository 기반 fallback
- `/steer [--job <id>] <instruction>`: 실행 중 추가 지시
- `/send ...`: `/steer` alias
- `/stop [job-id]`: Job 취소

Human Gate 응답은 별도 slash command가 아니라 Telegram inline button 또는 선택 키 메시지를 사용합니다.

## 장애/복구

R3까지 지원:

- Desktop disconnect 감지
- remote execution 실패 처리
- Codex external session 저장
- explicit session resume
- resume 실패 시 새 session fallback
- resume 결과 thread id 불일치 감지와 rebind
- pause와 stop 분리
- Human Gate runtime persistence
- Human decision → same-session resume

아직 R4에서 처리할 항목:

- `WAITING_HOST` 자동 재배치
- `WAITING_QUOTA`와 backoff
- Controller 재시작 후 running/waiting Job reconcile
- Approval `expires_at` 자동 처리
- stale Job recovery

Codex session은 최적화 수단이며 Source of Truth가 아닙니다. session resume가 불가능하면 repository filesystem 상태를 다시 읽고 새 session으로 진행합니다.

## 보안

- Telegram numeric user ID allowlist
- Approval은 원래 Job을 요청한 Telegram user ID만 응답 가능
- Controller/Runner transport 인증
- Desktop → Controller outbound only
- Messenger → shell 직접 실행 경로 없음
- Human Gate 선택도 shell 명령이 아니라 Codex instruction으로만 전달
- Project path는 server-side registry에서 결정
- Codex 인증정보는 각 Host에만 저장
- 기본 Codex sandbox는 `workspace-write`

HTTP API는 기본적으로 `127.0.0.1`에 bind합니다. Public Internet에 unrestricted 상태로 노출하지 마세요. 외부 네트워크에서는 `wss://` 또는 private network를 사용하세요.

## 상세 Architecture

- [Architecture](docs/ARCHITECTURE.md)
- [Sessions and Feedback](docs/SESSIONS_AND_FEEDBACK.md)
- [Human Gate](docs/HUMAN_GATE.md)
- [Messaging](docs/MESSAGING.md)
- [Runners](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)
- [Security](docs/SECURITY.md)
- [R2 Manual Smoke Test](docs/SMOKE_TEST_R2.md)
- [R3 Manual Smoke Test](docs/SMOKE_TEST_R3.md)

## Roadmap

- [x] R0 — Telegram + Lightsail Codex
- [x] R1 — Desktop Runner / WebSocket / heartbeat
- [x] R2 — Session resume / steering / progress feedback
- [x] R3 — Human Gate
- [ ] R4 — retry / quota / restart recovery
- [ ] R5 — Project OS adapter
- [ ] R6 — Slack / Web UI
