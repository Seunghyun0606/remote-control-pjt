# Automated QA

Remote Control 0.16부터 Codex 개발 turn이 성공하면 프로젝트의 Project OS QA Contract를 탐색하고, 지원되는 경우 Job 완료 전에 자동 QA를 실행합니다.

## Architecture

```text
Codex development
        ↓
Codex return code 0
        ↓
.qa/manifest.yaml detection (Contract v2)
        ↓
host OS command selection
        ↓
QA runner
        ↓
manifest-declared result.json
        ↓
contract validation + registered artifact collection
        ↓
PASS / PASS_WITH_WARNINGS / FAIL / HUMAN_GATE_REQUIRED
        ↓
Telegram summary + screenshots
        ↓
Job completion / failure / Human Gate
```

Remote Control은 Playwright selector, Godot scene, Android ADB command, Chrome Extension scenario를 알지 않습니다. 프로젝트별 테스트 구현은 각 repository에 있고 Remote Control은 Manifest/Result/Artifact contract만 소비합니다.

## Project OS Contract

현재 source of truth는 **Project OS 0.4.0 / QA Contract 2.0**입니다.

Discovery:

```text
.qa/manifest.yaml
```

Manifest가 제공하는 항목:

- `schemaVersion: "2.0"`
- `qa.command.windows` / `qa.command.unix`
- `qa.stages`
- optional `qa.timeoutSeconds`
- `artifacts.result`
- screenshot/log/visual artifact locations

Remote Control은 안전한 `runId`를 만들고 manifest command의 `{runId}`를 치환해 실행합니다.

Legacy compatibility:

- v2 manifest가 있으면 v2를 우선합니다.
- manifest가 없고 `scripts/qa.ps1`이 있으면 legacy Result Contract 1.0 adapter를 사용합니다.
- 지원하지 않는 schema version은 silent fallback 없이 실패합니다.

## Quick Start

Project OS 0.4.0 이상에서:

```powershell
projectctl qa-init
```

또는 신규 프로젝트:

```powershell
projectctl init --with-qa
```

생성되는 `.qa/manifest.yaml`과 OS별 runner를 프로젝트에 맞게 구현합니다. 기본 scaffold는 실제 QA 구현 전까지 `QA_NOT_CONFIGURED`로 fail-closed 합니다.

그 다음 Remote Control을 재시작하고 Telegram Project Topic에서 평소처럼 작업을 요청합니다.

```text
다음 작업 진행해줘
```

조회:

```text
/qa
```

QA만 재실행:

```text
/qa rerun
```

`/qa rerun`은 Codex 개발 turn을 다시 실행하지 않습니다.

## QA Lifecycle

Top-level Job state machine은 기존 값을 유지합니다. QA 내부 lifecycle은 `qa_runs.phase`에 저장합니다.

```text
QA_PREPARING
→ QA_RUNNING
→ QA_COLLECTING
→ QA_DONE
   or QA_FAILED
   or QA_REVIEWING
```

Result mapping:

| Project OS QA status | Remote Control |
|---|---|
| `PASS` | 기존 completion / Project OS submit 계속 |
| `PASS_WITH_WARNINGS` | completion 계속 + warning metadata/report |
| `FAIL` | Job `FAILED` |
| `HUMAN_GATE_REQUIRED` | Job `WAITING_HUMAN` + 기존 Human Gate |
| QA contract 없음 | 기존 QA 미지원 프로젝트와 동일하게 완료 |

Legacy v1의 `UI_REVIEW_REQUIRED`도 Human Gate로 계속 호환됩니다.

## Telegram Reports

QA 시작 시 시작 메시지를 보내고, 결과 시 요약을 전송합니다. v2에서는 고정 stage 이름을 가정하지 않고 `stages[]`와 `summary`를 읽습니다.

예:

```text
[desktown / JOB-...]

⚠️ Automated QA PASS_WITH_WARNINGS

Run: QA-...
Passed: 19
Failed: 0
Warnings: 1
Skipped: 0
Human Gates: 0
Stages: build=PASS, smoke=PASS, ui=WARN, visual=WARN
Artifacts: 4
```

Screenshot은 `artifacts[type=screenshot]` 중 우선순위에 따라 선택합니다.

```text
priority: failure > important > normal
kind:     failure > result > checkpoint > initial
```

최대 전송 수는 `QA_TELEGRAM_MAX_SCREENSHOTS`로 제한합니다.

## Screenshot / Artifact Security

Remote Control은 workspace를 임의 탐색해 PNG를 보내지 않습니다. Result에 등록된 artifact만 처리합니다.

v2 artifact path는 **repository root 기준 상대경로**입니다.

금지:

- absolute path
- drive-qualified path
- backslash path
- `..` traversal
- workspace 밖 resolved path
- unsupported screenshot type
- file/total size limit 초과

지원 screenshot 형식:

- PNG
- JPEG
- WebP

## Visual QA

Project OS v2의 `visualReviews[]`를 읽어 warning/issue 수를 Telegram report에 반영할 수 있습니다.

Visual review provider는 Remote Control core와 분리합니다. 향후 OpenAI, Claude 또는 다른 reviewer를 붙일 수 있지만 provider 하나와 강결합하지 않습니다.

AI visual warning 하나만으로 deterministic FAIL을 만들지 않습니다. Project runner가 `PASS_WITH_WARNINGS` 또는 `HUMAN_GATE_REQUIRED`로 표현한 contract status를 따릅니다.

## Failure Handling

최종 판단의 source of truth는 exit code가 아니라 `result.json.status`입니다. Exit code는 status와 일치하는지 fail-closed 검증하는 transport hint입니다.

v2:

| Exit | Status |
|---|---|
| 0 | PASS / PASS_WITH_WARNINGS |
| 1 | FAIL |
| 2 | HUMAN_GATE_REQUIRED |

다음은 orchestration failure입니다.

- malformed/missing manifest
- unsupported manifest/result schema version
- host OS command 없음
- malformed/missing result.json
- runId mismatch
- unsafe artifact path
- missing registered artifact
- unsupported screenshot format
- artifact size limit 초과
- result status / exit-code mismatch
- QA timeout

## Human Gate

`HUMAN_GATE_REQUIRED`이면 screenshot/artifact를 먼저 전달하고 기존 Approval Registry를 사용합니다.

```text
APPROVE → review_status=UI_APPROVED → Job completion 계속
REJECT  → review_status=UI_REJECTED → Job FAILED
```

Review status 명칭은 기존 DB 호환을 위해 유지하지만 v2 gate는 UI 전용이 아니라 일반 QA Human Gate로 사용합니다.

## Recovery

QA는 Codex session과 별도 실행이지만 같은 Job에 속합니다.

Controller/Runner가 QA 중 끊기면 `RecoveryMode.QA`로 전환합니다.

1. 동일 run id의 기존 result를 먼저 collect합니다.
2. 유효하면 QA command를 중복 실행하지 않고 후처리를 재개합니다.
3. result가 없거나 불완전하면 동일 run id로 runner를 다시 실행합니다.
4. Codex session을 QA 때문에 resume하지 않습니다.

Telegram final report/screenshot 전송 시각도 저장해 재시작 후 중복 전송을 줄입니다.

## Configuration

```dotenv
QA_ENABLED=true
QA_TIMEOUT_SECONDS=900
QA_TELEGRAM_SCREENSHOTS=true
QA_TELEGRAM_MAX_SCREENSHOTS=6
QA_ARTIFACT_MAX_BYTES=8388608
QA_ARTIFACT_TOTAL_MAX_BYTES=8388608

# bounded auto-fix extension point; default disabled
QA_AUTO_FIX=false
QA_AUTO_FIX_MAX_ATTEMPTS=2
```

Manifest의 `qa.timeoutSeconds`가 더 짧으면 해당 값을 사용하며 Remote Control 설정은 상한 역할을 합니다.

## Job Detail

`/job JOB-...`과 `/qa`에서 다음을 확인할 수 있습니다.

- QA status / phase
- QA run id
- failed scenarios
- warning count
- artifact count
- result path
- Human Gate review status

## Troubleshooting

### QA가 자동 실행되지 않음

v2:

```powershell
Test-Path .\.qa\manifest.yaml
```

legacy v1:

```powershell
Test-Path .\scripts\qa.ps1
```

### PASS_WITH_WARNINGS가 FAILED가 됨

Remote Control 0.16의 최신 branch가 Project OS Contract v2를 지원하는지 확인합니다. v2에서는 exit 0 + `PASS_WITH_WARNINGS`가 정상 completion입니다.

### screenshot이 전송되지 않음

- artifact가 result의 `artifacts[]`에 등록됐는지
- `type=screenshot`인지
- repository-relative forward-slash path인지
- v2 screenshot에 `caption`, `kind`, `priority`가 있는지
- 파일 형식/크기 제한을 넘지 않는지
- `QA_TELEGRAM_SCREENSHOTS=true`인지

## Extension Points

현재 core와 분리된 향후 확장 지점:

- bounded Codex auto-fix loop
- pluggable AI Visual Reviewer
- log/video Telegram delivery
- richer stage progress editing

무한 QA → fix → QA loop는 만들지 않습니다.
