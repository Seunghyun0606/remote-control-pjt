# R4 Manual Smoke Test

This checklist verifies recovery behavior with real Controller/Runner processes. Use a disposable repository for failure injection.

## 1. Baseline

Start Controller and Desktop Runner.

```text
/hosts
```

Expected: `lightsail-main` and/or `desktop-main` are ONLINE as configured.

Check:

```text
GET /recovery
```

Expected: no stale terminal recovery entries.

## 2. Explicit offline Host

Stop the Desktop Runner, then:

```text
/run <project> --host desktop-main
```

Expected:

- Job = `WAITING_HOST`
- Recovery kind = `HOST`
- Job does not immediately fail

Start Desktop Runner again.

Expected within the Scheduler cycle:

- Host = ONLINE
- Job leaves `WAITING_HOST`
- work starts on `desktop-main`

## 3. Auto fallback

Configure the same disposable project with paths for both Desktop and Lightsail. Set Desktop as default, leave Desktop offline and Lightsail online.

Run with `--host auto`.

Expected: HostRouter selects the online configured fallback instead of waiting for the offline default.

## 4. Heartbeat expiry

Stop or network-isolate the Desktop Runner without changing Controller DB manually.

Wait longer than:

```dotenv
REMOTE_CONTROL_HEARTBEAT_TIMEOUT_SECONDS=45
```

Expected: Scheduler persists Desktop status as OFFLINE and emits `HOST_OFFLINE`.

## 5. Quota recovery

Do not intentionally consume production quota merely to trigger this test.

For deterministic validation use the automated fake-runner test suite. If a real Codex usage limit naturally occurs, verify:

- Job → `WAITING_QUOTA`
- `GET /recovery` shows attempt and `next_retry_at`
- if a reset timestamp is available it is preferred
- otherwise configured backoff is used
- Scheduler later resumes the Job
- repeated quota waits increase backoff up to the configured maximum

## 6. Controller restart with remote job

Start a long-running disposable Job on Desktop.

While Codex is still running, restart only the Controller. Keep `remote-runner` alive.

Expected:

1. Controller startup reconciles persisted active Job to recovery state
2. Desktop reconnects
3. Runner sends `RUNNING_JOBS`
4. existing `execution_id` is matched
5. Controller emits `RUNNER_JOB_ADOPTED`
6. no duplicate `JOB_START` is sent
7. the original execution eventually completes normally

## 7. Controller restart without surviving execution

Start a disposable Job, then stop both Controller and Runner so the Runner process no longer retains its active handle. Start Controller and Runner again.

Expected after restart grace:

- no execution is adoptable
- Controller attempts existing Codex session resume
- if session resume fails, repository-state fallback starts a new session
- already-present filesystem changes are inspected before continuation

## 8. Approval expiry

Create a test Human Gate with an `expires_at` in the near future.

Expected after Scheduler tick:

- Approval = `EXPIRED`
- Job = `FAILED`
- `HUMAN_GATE_EXPIRED` event exists
- no default approval option is chosen

## 9. Stop during recovery

While a Job is `WAITING_HOST` or `WAITING_QUOTA`:

```text
/stop <job-id>
```

Expected:

- Job = `CANCELLED`
- RecoveryRecord is removed
- Scheduler does not restart the Job later

## Automated coverage

CI covers:

- quota text/structured reset parsing
- 30m → 1h → 2h backoff cap
- quota attempt persistence and automatic retry
- explicit Host offline wait / redispatch
- auto Host fallback
- heartbeat expiry persistence
- local Controller restart session recovery
- remote execution adoption
- disconnect classification as recoverable Host failure
- Human Gate expiry
- all R0-R3 regression tests

A real Telegram + real Codex + real Desktop/Lightsail E2E still requires those runtime credentials and hosts.
