# R2 Manual Smoke Test

This checklist requires a real Codex-authenticated Host and is intentionally separate from CI, which uses `FakeAgentRunner`.

## 1. Start Controller

Configure a real project path and start:

```bash
remote-control controller start
```

Verify in Telegram:

```text
/hosts
```

Expected: the intended Host is `ONLINE`.

## 2. Start a Job

```text
/run <project> --host lightsail-main
```

or:

```text
/run <project> --host desktop-main
```

Then:

```text
/status
/job <job-id>
```

Expected:

- state becomes `RUNNING`
- external Codex Session appears after `thread.started`
- `GET /sessions` contains the Session Registry record

## 3. Progress feedback

Use a task long enough to emit multiple Codex events.

Expected:

- raw low-level events are not all sent to Telegram
- progress feedback is rate-limited
- command failures, if any, are surfaced as important feedback
- Event Ledger still contains agent events

## 4. Steering

While the Job is RUNNING:

```text
UI는 건드리지 말고 backend만 수정해
```

Expected immediate response:

```text
추가 지시 접수
```

Expected runtime behavior:

1. current Codex turn finishes
2. `STEERING_APPLIED` is recorded
3. same external session is resumed
4. remote Host uses `JOB_STEER`
5. final Job completes after the steering turn

## 5. Pause / Resume

Start another sufficiently long Job, then:

```text
/pause
```

Expected:

- Job = `PAUSED`
- active process stops
- Session Registry status = `PAUSED`
- Job is not `CANCELLED`

Then:

```text
/resume
```

Expected:

- Job returns to `RUNNING`
- Controller attempts the same external Codex session
- Session becomes `ACTIVE`, then `IDLE` on successful completion

## 6. Stop distinction

Start a Job and run:

```text
/stop
```

Expected:

- Job = `CANCELLED`
- it cannot be resumed with the R2 `/resume` path

## 7. Desktop path

Repeat steering and pause/resume on `desktop-main`.

Expected:

- Desktop remains outbound-only
- WebSocket messages include `JOB_RESUME` or `JOB_STEER`
- Codex authentication remains local to Desktop

## CI coverage

Automated tests cover:

- Session registration/rebind
- Codex resume command generation
- pause/resume
- resume fallback
- steering queue
- remote `JOB_STEER`
- remote cancel unblock
- feedback throttling

CI cannot prove a user's real Codex login/session files or Telegram credentials, so those checks remain manual.
