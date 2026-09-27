# Remote Control User Guide

이 문서는 **Remote Control 0.15.4** 기준의 실사용 가이드입니다.

목표는 설치 세부사항보다 다음 흐름을 빠르게 이해하는 것입니다.

```text
원격 작업 요청
→ Codex 실행
→ 같은 Session에서 계속 작업
→ 작업 중 추가 지시 / 즉시 방향전환
→ 여러 작업 Queue
→ Human Gate
→ Host / Quota / Controller 재시작 복구
→ 결과 확인
```

설치와 환경변수 구성은 프로젝트 루트 `README.md`를 참고하세요.

---

## 1. 가장 먼저 확인할 것

Controller를 시작하기 전에 실행환경을 확인합니다.

```powershell
remote-control --version
remote-control doctor
```

정상적인 0.15.4 설치라면:

```text
0.15.4
```

가 표시됩니다.

Controller 실행:

```powershell
remote-control controller start
```

Telegram Bot에서 최초 확인:

```text
/start
/sync
/projects
/hosts
/status
```

Project Topic을 사용하는 경우 `/sync` 후 각 프로젝트별 Topic이 생성되거나 기존 mapping이 갱신됩니다.

---

## 2. 프로젝트 작업 시작

### Project Topic에서

해당 프로젝트 Topic에서는 프로젝트 ID를 다시 적을 필요가 없습니다.

```text
/run
```

또는 일반 문장을 바로 보낼 수 있습니다.

```text
현재 상태 확인하고 다음 작업 진행해줘
```

현재 프로젝트에 active Job이 없으면 이 문장으로 새 Codex Job을 시작합니다.

### Bot 기본 대화에서

프로젝트를 명시합니다.

```text
/run project-os
```

특정 Host를 지정하려면:

```text
/run project-os --host desktop-main
```

등록된 프로젝트 확인:

```text
/projects
```

실행 가능한 Host 확인:

```text
/hosts
```

---

## 3. 같은 Codex Session에서 계속 작업

Remote Control은 사용자 + 프로젝트 단위의 **Project Session**을 유지합니다.

```text
Job 1
 ↓
Codex Session A

다음 작업
 ↓
Codex Session A resume

추가 작업
 ↓
Codex Session A resume
```

따라서 매 Job마다 README, Git 상태, 이전 결정 등을 처음부터 다시 읽는 비용을 줄일 수 있습니다.

현재 Session:

```text
/session
```

최근 Session 목록:

```text
/sessions
```

새 Session으로 전환:

```text
/session new
```

과거 Project Session 재사용:

```text
/session use <project-session-id>
```

같은 머신에서 Codex CLI로 직접 이어서 보고 싶다면 `/session`에 표시되는 Codex session ID를 사용할 수 있습니다.

```powershell
codex resume --include-non-interactive <codex-session-id>
```

이 기능은 Remote Control과 직접 실행하는 Codex가 같은 `CODEX_HOME`을 사용할 때 가장 자연스럽게 동작합니다.

---

## 4. 실행 중 작업에 추가 지시

### /steer — 현재 작업은 계속하고 다음 Turn에 반영

```text
/steer 테스트 케이스도 추가해줘
```

특정 Job을 지정하려면:

```text
/steer --job JOB-... 테스트도 추가해줘
```

동작:

```text
현재 Codex Turn 계속
→ Turn 종료
→ 같은 Codex Session의 다음 Turn
→ 추가 지시 적용
```

Project Topic에서 active Job이 정확히 하나라면 일반 메시지를 보내는 것도 `/steer`와 같은 용도로 사용할 수 있습니다.

```text
README도 같이 수정해줘
```

---

## 5. /redirect — 현재 방향을 즉시 바꾸기

현재 작업 방향 자체가 잘못됐다면 `/redirect`를 사용합니다.

```text
/redirect DB 구조부터 다시 검토하고 그 방향으로 진행해
```

특정 Job:

```text
/redirect --job JOB-... UI 작업은 중단하고 backend부터 수정해
```

동작:

```text
현재 Codex Turn
→ 안전하게 종료
→ 동일 Codex Session 유지
→ 새 instruction으로 즉시 resume
```

차이:

```text
/steer
  현재 작업은 유효함
  → 끝난 뒤 지시 추가

/redirect
  현재 방향을 계속하면 안 됨
  → 현재 Turn 종료 후 즉시 새 방향
```

---

## 6. Pause / Resume / Stop

일시정지:

```text
/pause
```

특정 Job:

```text
/pause JOB-...
```

재개:

```text
/resume
```

중지:

```text
/stop
```

`/pause`는 OS process를 그대로 freeze하는 기능이 아닙니다.

```text
현재 Codex Turn 종료
+
Project Session 유지
```

이후 `/resume`하면 같은 Session에서 이어서 작업합니다.

---

## 7. 여러 작업을 Queue에 넣기

같은 working tree에서 이미 Job A가 실행 중이어도 추가 요청은 실패하지 않고 대기할 수 있습니다.

```text
Job A  RUNNING
Job B  WAITING_LEASE
Job C  WAITING_LEASE
```

앞 Job이 끝나면:

```text
A COMPLETED
→ B RUNNING

B COMPLETED
→ C RUNNING
```

`WAITING_LEASE` Job은 기다리는 동안 working-tree execution lease와 Project Session lock을 점유하지 않습니다.

자기 차례가 되면:

1. working-tree lease 획득
2. 최신 Project Session 재획득
3. 최신 Codex thread를 이어서 실행

순으로 처리됩니다.

---

## 8. Telegram Queue 관리

현재 실행 중 작업과 대기 Queue를 확인합니다.

```text
/queue
```

예:

```text
🎛 project-os 작업 현황

현재
• JOB-A RUNNING

대기열
1. JOB-B
   테스트 추가

2. JOB-C
   README 정리
```

Telegram inline 버튼:

```text
⬆  순서 올리기
⬇  순서 내리기
✕  대기 Job 취소
🔄 새로고침
```

Queue 순서는 DB의 `recovery.queue_position`에 저장되므로 Controller 재시작 후에도 유지됩니다.

Queue 순서는 실제 execution lease lane인 **Host + canonical working directory** 단위로 관리됩니다. 서로 다른 checkout은 불필요하게 한 Queue로 직렬화하지 않고 병렬 실행할 수 있습니다.

---

## 9. Job 상태 조회

현재 active Job:

```text
/status
```

최근 Job 목록:

```text
/jobs
```

특정 Job 상세:

```text
/job JOB-...
```

상세 정보에서는 다음과 같은 내용을 확인할 수 있습니다.

- Job State
- Project
- Host
- Codex Session
- Recovery 종류
- Retry 횟수
- 다음 Retry 시각
- 마지막 오류

---

## 10. 재시도

```text
/retry JOB-...
```

상태별 동작은 다릅니다.

| State | /retry 동작 |
|---|---|
| `FAILED` | 기존 실패 기록은 유지하고 새 Retry Job 생성 |
| `WAITING_HOST` | Host 사용 가능 여부를 즉시 다시 확인 |
| `WAITING_LEASE` | working-tree lease 획득을 즉시 재시도 |
| `WAITING_QUOTA` | 예약 시각을 기다리지 않고 즉시 resume 시도 |
| `WAITING_HUMAN` | Human Gate를 우회하지 않음 |

Project OS Job의 FAILED retry는 일반 clone 방식으로 처리하지 않습니다. Project OS canonical task state를 다시 읽을 수 있도록 새 `/run`을 사용하는 것이 기본 동작입니다.

---

## 11. 자동 Recovery

### Host 장애

```text
RUNNING
→ Host unavailable
→ WAITING_HOST
→ Host 복귀
→ 자동 실행 / resume
```

### Codex Quota

```text
RUNNING
→ usage/rate limit
→ WAITING_QUOTA
→ reset/retry 시각 도달
→ 동일 Codex Session resume
```

reset 시각을 알 수 없으면 기본 backoff를 사용합니다.

```text
1차: 30분
2차: 60분
3차 이후: 120분
```

### Controller 재시작

Controller가 재시작되면 DB의 runtime state를 기준으로 다음을 reconciliation합니다.

- Job
- Recovery
- Execution Lease
- Project Session
- Queue
- local process PID / process identity

복구 가능한 상태는 자동으로 다시 대기 또는 실행 상태로 연결합니다.

---

## 12. Human Gate

Codex가 사용자의 명시적인 결정이 필요하다고 판단하면 Job은:

```text
WAITING_HUMAN
```

상태로 들어갑니다.

예:

```text
A. 기존 DB 유지
B. migration 추가
C. 새 테이블 분리
```

Telegram 버튼이나 허용된 자연어 응답으로 선택하면:

```text
Human decision
→ 동일 Project/Codex Session
→ 작업 resume
```

Human Gate 상태에서는 `/retry`로 결정을 우회하지 않습니다.

---

## 13. Desktop / Lightsail / Remote Runner

기본 권장 구조는 각 머신이 독립 Controller가 되는 형태입니다.

```text
Desktop Telegram Bot
→ Desktop Controller
→ Desktop Codex
→ Desktop projects

Lightsail Telegram Bot
→ Lightsail Controller
→ Lightsail Codex
→ Lightsail projects
```

Host 확인:

```text
/hosts
```

특정 Host 실행:

```text
/run my-project --host desktop-main
```

Advanced 구성에서는 Controller와 다른 머신의 WebSocket Runner도 사용할 수 있습니다.

주의할 점은 Codex Session storage가 host-local이라는 것입니다. 기존 thread가 있는 Project는 가능한 한 같은 Host에서 이어가는 것이 안전합니다.

---

## 14. Project OS 연동

Project 등록:

```powershell
remote-control project add `
  --id my-project `
  --name "My Project" `
  --path "C:\projects\my-project" `
  --host desktop-main `
  --adapter project-os `
  --role developer
```

Project OS adapter는 canonical state를 직접 수정하지 않고 `projectctl`을 경계로 사용합니다.

준비 단계:

```text
projectctl status
projectctl next
projectctl context
projectctl claim
```

Codex 구현이 성공한 뒤:

```text
projectctl submit
```

을 통해 implementation handoff를 제출합니다.

Remote Control Job의 `COMPLETED`는 구현 turn과 handoff submit이 끝났다는 뜻입니다. Project OS task 자체의 최종 PASS/REWORK/HUMAN_GATE 평가는 Project OS가 별도로 결정합니다.

---

## 15. 진단 명령

CLI:

```powershell
remote-control --version
remote-control doctor
```

특정 env 파일을 명시하려면:

```powershell
remote-control doctor --env-file "C:\path\to\remote-control-pjt\.env"
remote-control controller start --env-file "C:\path\to\remote-control-pjt\.env"
```

Telegram:

```text
/doctor
```

주로 확인하는 항목:

- Host ID
- Codex executable
- Git executable
- Runtime Home
- DB 경로
- config 경로
- Project working directory

---

## 16. Job State 빠른 해석

| State | 의미 | 보통의 사용자 행동 |
|---|---|---|
| `RUNNING` | Codex 작업 중 | 기다리거나 `/steer`, `/redirect` |
| `WAITING_HOST` | Host 대기 | 보통 자동 복구 대기 |
| `WAITING_LEASE` | 앞선 working-tree Job 대기 | `/queue` 확인, 필요 시 순서 조정 |
| `WAITING_QUOTA` | Codex quota 대기 | 자동 복구 대기 또는 `/retry` |
| `WAITING_HUMAN` | 사용자 결정 필요 | Telegram 버튼/응답 |
| `PAUSED` | 사용자 일시정지 | `/resume` |
| `CANCELLING` | 실제 Codex 종료 확인 중 | 종료 확인 대기 |
| `FAILED` | 복구 불가 오류 | 원인 수정 후 `/retry` 또는 새 `/run` |
| `COMPLETED` | 정상 완료 | 다음 작업 진행 |
| `CANCELLED` | 중지 완료 | 필요 시 새 작업 |

---

## 17. 권장 실사용 흐름

가장 단순한 운영 패턴입니다.

```text
1. remote-control controller start

2. Telegram Project Topic 이동

3. 일반 문장
   "현재 상태 확인하고 다음 작업 진행해줘"

4. 진행 중 보완
   /steer 테스트도 같이 추가해줘

5. 방향 변경
   /redirect 지금 방향은 중단하고 DB 구조부터 다시 검토해줘

6. 추가 작업을 계속 등록
   → WAITING_LEASE Queue

7. /queue
   → 순서 변경 / 대기 Job 취소

8. Human Gate 발생
   → Telegram에서 선택

9. Host / Quota 문제
   → 자동 Recovery

10. /status 또는 /job JOB-...
    → 결과와 상태 확인
```

---

## 18. 명령어 요약

### 프로젝트 / 시스템

```text
/start
/help
/projects
/sync
/hosts
/doctor
```

### 작업

```text
/run [project-id] [--host <host-id>]
/status
/queue
/jobs
/job <job-id>
/retry <job-id>
```

### Session

```text
/sessions
/session
/session new
/session use <project-session-id>
```

### 실행 중 제어

```text
/steer [--job <job-id>] <instruction>
/redirect [--job <job-id>] <instruction>
/pause [job-id]
/resume [job-id]
/stop [job-id]
```

Project Topic에서는 일반 문장도 중요한 인터페이스입니다.

- active Job 없음 → 새 Job
- steer 가능한 active Job 1개 → 추가 지시
- Human Gate 1개 → 선택지와 일치하면 Human Gate 응답
- active Job 여러 개 → Job 선택 필요

---

## 19. 관련 문서

더 깊은 동작은 다음 문서를 참고하세요.

- `README.md` — 설치와 전체 설정
- `docs/SESSIONS_AND_FEEDBACK.md` — Session, steer, redirect
- `docs/RECOVERY.md` — Host/Lease/Quota/Restart recovery
- `docs/HUMAN_GATE.md` — Human Gate
- `docs/TELEGRAM_QUEUE_MANAGEMENT_015.md` — Telegram Queue 관리
- `docs/PROJECT_ADAPTERS.md` — Generic Git / Project OS
- `docs/RUNNERS.md` — Local / Remote Runner
- `docs/ARCHITECTURE.md` — 전체 구조
