# Remote Agent Control

Messenger에서 원격 머신의 AI coding agent를 실행하고, 상태를 확인하고, 중지하고, 이후에는 추가 지시와 Human Gate까지 처리하기 위한 Remote Agent Control Plane입니다.

이 저장소는 **Project OS와 독립적**입니다. Project OS는 "무엇을 해야 하는가"를 관리하고, Remote Agent Control은 "어디에서 실행하고 사용자와 어떻게 소통할 것인가"를 관리합니다. Project OS 연동은 이후 optional adapter로만 추가합니다.

## 현재 구현 범위

현재 목표는 **Phase R0 — Single Host Prototype**입니다.

```text
Telegram
   ↓
Controller
   ↓
SQLite Runtime DB / Job Manager
   ↓
Lightsail Local Runner
   ↓
Codex CLI
```

R0에서 제공하는 기능:

- Telegram allowlist
- `/projects`, `/status`, `/run`, `/jobs`, `/job`, `/stop`
- 간단한 자연어 실행 명령
- Project Registry
- SQLite runtime state
- Job state validation
- Lightsail local runner
- Codex CLI adapter
- structured runtime events
- FakeAgentRunner 기반 자동화 테스트

> Desktop Runner, WebSocket host registry, session resume, steering, Human Gate, quota recovery, Project OS adapter는 R1~R5에서 순차적으로 추가합니다.

## Quick Start

### 1. 요구사항

- Python 3.11+
- Git
- Codex CLI
- Telegram Bot Token

Codex CLI 인증은 **실행 Host에서 직접** 해두어야 합니다. ChatGPT/Codex 인증정보를 Controller에 복사하지 않습니다.

### 2. 설치

```bash
git clone https://github.com/Seunghyun0606/remote-control-pjt.git
cd remote-control-pjt

python -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. 환경 설정

`.env`에서 다음 값을 설정합니다.

```dotenv
REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./remote-control.db
REMOTE_CONTROL_CONFIG=./config/projects.yaml
REMOTE_CONTROL_HOST_ID=lightsail-main

TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=
```

`TELEGRAM_ALLOWED_USER_IDS`에는 Telegram numeric user id만 넣습니다. username은 권한 확인에 사용하지 않습니다.

### 4. 프로젝트 등록

`config/projects.yaml`:

```yaml
projects:
  dailytown:
    name: DailyTown
    adapter: generic_git
    repository:
      path:
        lightsail-main: /home/codex/projects/dailytown
    allowed_hosts:
      - lightsail-main
    default_host: lightsail-main
```

### 5. Controller 시작

```bash
remote-control controller start
```

Telegram 없이 API/로컬 smoke test만 하려면:

```bash
remote-control controller start --no-telegram
```

### 6. 테스트

```bash
pytest
```

## Telegram에서 명령하는 방법

R0 명령:

```text
/projects
/status
/run <project>
/run <project> --host lightsail-main
/jobs
/job <job-id>
/stop
```

자연어 예:

```text
DailyTown 다음 작업 진행해
```

R0에서는 자연어를 shell command로 변환하지 않습니다. 검증된 프로젝트 실행 intent만 생성합니다.

## Desktop Runner 설치

Desktop Runner는 **Phase R1**에서 추가합니다. 설계상 Desktop은 inbound port를 열지 않고 Controller에 outbound WebSocket으로 연결합니다.

## Lightsail Runner 설치

R0에서는 Controller 프로세스 안의 local runner가 Codex CLI를 실행합니다. Controller와 runner의 인터페이스는 분리되어 있어 R1에서 원격 WebSocket runner로 교체할 수 있습니다.

실행 Host에서 먼저 Codex CLI 인증을 완료합니다.

```bash
codex --version
```

Controller는 Codex의 개인 인증 토큰이나 `~/.codex/auth.json`을 저장하지 않습니다.

## 프로젝트 등록

프로젝트별 working directory는 allowlist된 host path로만 지정합니다. job 시작 전에 프로젝트 경로가 실제로 존재하는지 확인하며, 이후 단계에서는 dirty working tree/Human Gate 정책을 강화합니다.

## 명령 목록

- `/projects`: 등록 프로젝트
- `/status`: Controller와 active job 요약
- `/run <project>`: 프로젝트 job 생성
- `/jobs`: 최근 job
- `/job <id>`: job 상세 상태
- `/stop`: 현재 사용자의 active job 중지

## 장애/복구

R0는 process-level failure를 `FAILED` 상태와 event ledger에 기록합니다. Controller 재시작 복구, quota wait/retry, offline host redispatch는 R4에서 구현합니다.

## 보안

- Telegram numeric user id allowlist를 사용합니다.
- Telegram bot이 shell을 직접 실행하지 않습니다.
- 모든 실행은 Controller가 만든 validated Job을 거쳐 Runner로 전달됩니다.
- 자연어를 shell command로 그대로 전달하지 않습니다.
- Codex 인증정보는 각 Runner Host에만 둡니다.
- 프로젝트 working directory 밖에서 agent를 실행하지 않습니다.
- 운영 credential/SSH key/home directory 전체를 agent workspace로 노출하지 않습니다.

## 상세 Architecture

- [Architecture](docs/ARCHITECTURE.md)
- [Messaging](docs/MESSAGING.md)
- [Runner](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)
- [Security](docs/SECURITY.md)

## Roadmap

1. R0 — Telegram + Lightsail Codex
2. R1 — Desktop Runner / WebSocket / heartbeat
3. R2 — Session resume / steering / progress feedback
4. R3 — Human Gate
5. R4 — retry / quota / restart recovery
6. R5 — Project OS adapter
7. R6 — Slack / Web UI

