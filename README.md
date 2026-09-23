# Remote Agent Control

Messenger에서 원격 머신의 AI coding agent를 실행하고, 상태를 확인하고, 실행 Host를 선택하고, 결과를 다시 받을 수 있게 하는 Remote Agent Control Plane입니다.

Project OS와는 독립 프로젝트이며, Project OS 연동은 이후 optional adapter로만 추가합니다.

## 현재 구현

R0 + R1까지 구현되어 있습니다.

```text
Telegram
   ↓
Controller
   ├─ SQLite Job / Event / Host State
   ├─ Lightsail Local Runner → Codex CLI
   └─ WebSocket Gateway
            ↑ outbound
       Desktop Runner → Codex CLI
```

현재 지원:

- Telegram allowlist
- `/projects`, `/status`, `/hosts`, `/run`, `/jobs`, `/job`, `/stop`
- deterministic 자연어 실행 명령
- Job state validation
- Lightsail local Codex execution
- Desktop outbound WebSocket Runner
- Host register / heartbeat / offline 처리
- explicit host 선택 / auto routing
- 자동화 테스트와 CI

R2 이후의 session resume/steering, Human Gate, quota/restart recovery, Project OS adapter는 아직 포함하지 않습니다.

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

Controller의 `.env`에서 Telegram 설정과 Controller/Runner 연결용 secret을 설정한 뒤:

```bash
remote-control controller start
```

Telegram 없이 Controller만 실행하려면:

```bash
remote-control controller start --no-telegram
```

## Telegram에서 명령하는 방법

```text
/projects
/status
/hosts
/run <project>
/run <project> --host lightsail-main
/run <project> --host desktop-main
/jobs
/job <job-id>
/stop
```

자연어 예:

```text
DailyTown 다음 작업 진행해
DailyTown desktop에서 다음 작업 진행해
```

자연어는 shell command로 변환하지 않습니다.

## Desktop Runner 설치

Windows에서:

```powershell
.\runner\windows\install.ps1
```

Desktop `.env`의 주요 설정:

```dotenv
REMOTE_RUNNER_CONTROLLER_WS=wss://YOUR-CONTROLLER/ws/runner
REMOTE_RUNNER_TOKEN=<controller와 동일한 transport secret>
REMOTE_RUNNER_HOST_ID=desktop-main
REMOTE_RUNNER_NAME=Main Desktop
REMOTE_RUNNER_OS=windows
REMOTE_RUNNER_CAPABILITIES=codex,git,android,gui,browser
```

Desktop 계정에서 Codex CLI 인증을 완료한 뒤:

```powershell
.\.venv\Scripts\remote-runner.exe start
```

Desktop은 Controller로 outbound WebSocket만 생성합니다. Desktop에 inbound port를 열지 않습니다.

## Lightsail Runner 설치

Controller Host에서는 Codex를 local runner로 실행합니다. Controller Host의 Codex CLI는 그 Host에서 직접 인증해 둡니다.

## 프로젝트 등록

`config/projects.yaml`에서 Host별 실제 repository 경로를 등록합니다.

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

`/hosts`로 online 상태를 확인한 뒤 원하는 Host를 지정해 실행할 수 있습니다.

## 장애/복구

R1은 Runner disconnect와 Host OFFLINE을 감지하고 연결이 끊긴 remote run을 실패 처리합니다.

WAITING_HOST 자동 재배치, quota retry, Controller restart reconciliation은 R4에서 추가합니다.

## 보안

- Telegram numeric user ID allowlist
- Controller/Runner transport 인증
- Desktop → Controller outbound only
- Telegram → shell 직접 실행 금지
- Messenger 입력으로 임의 filesystem path 지정 금지
- Codex 인증정보는 각 Host에만 보관
- Codex 기본 sandbox는 `workspace-write`

외부 네트워크를 통과한다면 `wss://` 또는 private network 사용을 권장합니다.

## 테스트

```bash
ruff check .
pytest
```

## 상세 문서

- [Architecture](docs/ARCHITECTURE.md)
- [Messaging](docs/MESSAGING.md)
- [Runners](docs/RUNNERS.md)
- [Protocol](docs/PROTOCOL.md)
- [Security](docs/SECURITY.md)

## Roadmap

- [x] R0 — Telegram + Lightsail Codex
- [x] R1 — Desktop Runner / WebSocket / heartbeat
- [ ] R2 — Session resume / steering / progress feedback
- [ ] R3 — Human Gate
- [ ] R4 — retry / quota / restart recovery
- [ ] R5 — Project OS adapter
- [ ] R6 — Slack / Web UI
