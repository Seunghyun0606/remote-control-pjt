# Automated QA

Remote Control 0.16부터 Codex 개발 turn이 성공하면, 프로젝트가 Project OS QA Contract 1.0을 제공하는 경우 Job을 완료하기 전에 자동 QA를 실행합니다.

## Architecture

```text
Codex development
        ↓
Codex return code 0
        ↓
Project QA capability detection
        ↓
scripts/qa.ps1 -RunId QA-...
        ↓
.qa/runs/<run-id>/result.json
        ↓
Contract validation + registered artifact collection
        ↓
PASS / FAIL / UI_REVIEW_REQUIRED
        ↓
Telegram summary + screenshots
        ↓
Job terminal state or Human Gate
```

Remote Control은 Playwright selector, Godot scene, Android ADB command, Chrome Extension test scenario를 알지 않습니다. 프로젝트별 QA 구현은 각 프로젝트 repository에 남고 Remote Control은 Project OS가 정의한 공통 entry point/result/artifact contract만 소비합니다.

## Project OS Contract

현재 지원하는 source of truth는 Project OS 0.3.0의 QA Contract 1.0입니다.

```text
entry point: scripts/qa.ps1
run id:     -RunId QA-...
result:     .qa/runs/<run-id>/result.json
schema:     1.0
exit code:  0=PASS, 1=FAIL, 2=UI_REVIEW_REQUIRED
```

이전 설계 문서에서 제안했던 `.qa/manifest.yaml` 또는 `.qa/output/result.json`은 현재 Project OS 정본이 아니므로 사용하지 않습니다. 지원하지 않는 schema version은 silent fallback 없이 실패합니다.

## Quick Start

QA를 사용할 Project OS 프로젝트에서:

```powershell
projectctl qa-init
```

또는 신규 프로젝트:

```powershell
projectctl init --with-qa
```

그 다음 프로젝트에 맞게 `scripts/qa.ps1`을 구현합니다. 기본 scaffold는 `QA_NOT_CONFIGURED` FAIL을 반환하므로 실제 QA 구현 전에는 성공으로 처리되지 않습니다.

Remote Control을 최신 버전으로 다시 시작한 뒤 Telegram Project Topic에서 평소처럼 작업을 요청합니다.

```text
다음 작업 진행해줘
```

Codex가 성공하면 QA가 자동으로 이어집니다.

수동 조회:

```text
/qa
```

QA만 다시 실행:

```text
/qa rerun
```

`/qa rerun`은 Codex 개발 turn을 다시 실행하지 않습니다.

## QA Lifecycle

Top-level Job state machine은 기존 값을 유지합니다. QA 내부 상태는 `qa_runs.phase`에 저장합니다.

```text
QA_PREPARING
→ QA_RUNNING
→ QA_COLLECTING
→ QA_DONE
   or QA_FAILED
   or QA_REVIEWING
```

결과 매핑:

| Project OS QA status | Remote Control |
|---|---|
| `PASS` | 기존 completion / Project OS submit 계속 |
| `FAIL` | Job `FAILED`; Codex Project Session은 재사용 가능 |
| `UI_REVIEW_REQUIRED` | Job `WAITING_HUMAN`; Telegram UI 승인/거절 |
| entry point 없음 | 기존 QA 미지원 프로젝트와 동일하게 완료 흐름 계속 |

`UI_APPROVED` / `UI_REJECTED`는 Project OS QA result 상태가 아니라 Remote Control의 Human Gate review metadata입니다.

## Telegram Reports

QA가 시작되면 한 번 시작 메시지를 보내고, 결과가 생성되면 stage 요약을 보냅니다.

예:

```text
[desktown / JOB-...]

✅ Automated QA PASS

Run: QA-...
Preflight: PASS
Build: PASS
Launch: PASS
Smoke: PASS
Functional: PASS
UI: PASS
Artifacts: 4
```

Screenshot artifact가 있으면 최대 `QA_TELEGRAM_MAX_SCREENSHOTS`개를 Telegram media group으로 전송합니다. 각 caption에는 screenshot 이름, QA status, run id, scenario가 포함됩니다.

QA-specific final report와 screenshot 전송 완료 시각을 DB에 기록해 restart 후 동일 run의 중복 전송을 줄입니다.

## Screenshot Policy

Remote Control은 Project directory를 PNG 확장자로 탐색하지 않습니다. 반드시 `result.json.artifacts[]`에 등록된 `type=screenshot` artifact만 읽습니다.

기본 제한:

- run directory 기준 relative path만 허용
- absolute path 거부
- `..` traversal 거부
- backslash path 거부
- screenshot은 PNG/JPEG/WebP만 허용
- `QA_ARTIFACT_MAX_BYTES`보다 큰 screenshot 거부
- `QA_TELEGRAM_MAX_SCREENSHOTS`로 전송 개수 제한

Remote Runner를 사용할 때도 artifact validation/reading은 프로젝트가 존재하는 Runner host에서 수행하고 Controller에는 contract 결과와 허용된 screenshot bytes만 전달합니다.

## Failure Handling

QA command exit code만으로 PASS를 판단하지 않습니다. `result.json`이 반드시 존재해야 하며 schema/status와 exit code가 일치해야 합니다.

다음은 QA orchestration failure로 처리됩니다.

- malformed/missing result.json
- unsupported schema version
- run id mismatch
- invalid result status/stage
- unsafe artifact path
- missing registered artifact
- unsupported screenshot type
- artifact size limit 초과
- status/exit-code mismatch
- QA timeout

Deterministic QA `FAIL`은 Job을 `FAILED` 처리합니다.

## UI Review

자동 검증은 통과했지만 사람의 시각 판단이 필요하면 프로젝트 QA는:

```json
{
  "status": "UI_REVIEW_REQUIRED",
  "ui": "REVIEW_REQUIRED",
  "next_action": "REQUEST_UI_REVIEW"
}
```

을 반환합니다.

Remote Control은 screenshot을 보낸 뒤 기존 Human Gate를 사용합니다.

```text
APPROVE → UI_APPROVED → Job completion 계속
REJECT  → UI_REJECTED → Job FAILED
```

AI Visual Reviewer warning을 contract에 추가하려면 Project OS result의 `metadata` 확장 또는 향후 reviewer abstraction을 사용해야 합니다. Remote Control core는 특정 OpenAI/Claude provider와 결합하지 않습니다.

## Recovery

QA는 Codex session과 분리되지만 같은 Job에 속합니다.

Controller 또는 Runner 연결이 QA 중 끊기면 `RecoveryMode.QA`로 전환하고, host/lease recovery 후:

1. 동일 `run_id`의 기존 `result.json`을 먼저 수집합니다.
2. 유효한 result가 있으면 QA command를 중복 실행하지 않고 후처리를 재개합니다.
3. result가 없거나 불완전하면 같은 `run_id`로 QA를 다시 실행합니다.
4. Codex session을 잘못 resume하지 않습니다.

Remote Runner 쪽 project operation이 취소되면 QA subprocess tree도 종료하도록 구성했습니다.

## Job Detail

`/job JOB-...`에 다음 QA metadata가 추가됩니다.

- QA status/phase
- QA run id
- warning count
- artifact count
- result path
- UI review status

`/qa`는 해당 Project Topic에서 가장 최근 Job의 QA 상태를 간단히 표시합니다.

## Configuration

```dotenv
QA_ENABLED=true
QA_TIMEOUT_SECONDS=900
QA_TELEGRAM_SCREENSHOTS=true
QA_TELEGRAM_MAX_SCREENSHOTS=6
QA_ARTIFACT_MAX_BYTES=8388608

# Phase 4 bounded auto-fix용 예약 설정.
# 현재 기본값은 비활성이고 무한 QA→fix loop는 구현하지 않는다.
QA_AUTO_FIX=false
QA_AUTO_FIX_MAX_ATTEMPTS=2
```

## Troubleshooting

### QA가 자동 실행되지 않음

프로젝트 root에 `scripts/qa.ps1`이 있는지 확인합니다.

```powershell
Test-Path .\scripts\qa.ps1
```

### 기본 scaffold가 항상 FAIL

정상입니다. Project OS 기본 QA scaffold는 실제 프로젝트 QA가 구현되기 전에는 `QA_NOT_CONFIGURED`로 fail-closed 합니다.

### screenshot이 전송되지 않음

다음을 확인합니다.

- artifact가 `result.json.artifacts[]`에 등록됐는지
- `type`이 `screenshot`인지
- path가 run directory 기준 forward-slash relative path인지
- PNG/JPEG/WebP인지
- 크기 제한을 넘지 않았는지
- `QA_TELEGRAM_SCREENSHOTS=true`인지

### QA 중 Controller/Runner 재시작

Job이 `WAITING_HOST`에 들어갔다가 QA recovery로 이어질 수 있습니다. `/job`에서 Recovery와 QA run을 함께 확인하세요.

## Current Extension Points

향후 확장을 위해 다음은 core contract와 분리합니다.

- bounded Codex auto-fix loop
- AI Visual Reviewer provider interface
- screenshot prioritization metadata
- videos/log file direct Telegram upload

특정 provider나 프로젝트 framework를 Remote Control core에 넣지 않는 원칙을 유지합니다.
