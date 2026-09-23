# R5 Manual Smoke Test

Use a disposable Project OS project before validating production repositories.

## 1. Prerequisites

On the execution Host:

```bash
projectctl status --json
projectctl next --role developer --json
codex --version
```

The first two commands must run from the project checkout.

## 2. Register a Project OS project

```bash
remote-control project add \
  --id smoke-project \
  --path /absolute/path/to/smoke-project \
  --host lightsail-main \
  --adapter project-os \
  --role developer
```

Check:

```text
/projects
```

Expected: `smoke-project [project_os]`.

## 3. Run next work

```text
/run smoke-project
```

Expected sequence in runtime events/state:

1. Project OS status read
2. eligible Task selected
3. context package built
4. Task claimed
5. Codex started
6. successful turn enters `WAITING_AGENT`
7. implementation result submitted through `projectctl submit`
8. Remote Job becomes `COMPLETED`

Check:

```text
/job JOB-...
GET /jobs/{job_id}/project-work
```

Expected ProjectWork:

```text
adapter=project_os
host_id=<execution-host>
task_id=TASK-...
status=SUBMITTED
```

## 4. Verify Project OS semantics

Run directly in the project:

```bash
projectctl status --json
```

Expected:

- implementation handoff exists
- Remote Control did not directly edit canonical YAML
- the Task is not automatically marked PASS/done merely because the Remote Job completed

Review/evaluation remains a separate Project OS concern.

## 5. No eligible work

Use a project where `projectctl next --role developer --json` has no eligible Task.

Run:

```text
/run smoke-project
```

Expected:

- Remote Job completes with “no eligible Project OS work”
- Codex is not started
- no Task is invented by the Controller

## 6. Claim restart boundary

Create a disposable test where the Task has already become active but Remote Control ProjectWork still represents pre-claim selection, then restart the Controller.

Expected:

- status `current_tasks` is inspected
- duplicate `projectctl claim` is not required
- ProjectWork proceeds to PREPARED

The automated suite covers this deterministically.

## 7. Submit restart boundary

Start a disposable Project OS Job and restart the Controller while the Job is finalizing the implementation handoff.

Expected:

```text
WAITING_AGENT
→ WAITING_HOST
→ RecoveryMode.FINALIZE
→ submit retry
→ COMPLETED
```

Expected: Codex is not started a second time merely to re-submit the handoff.

## 8. Host pinning

Configure the project on Lightsail and Desktop, start on one Host, and force that Host offline after Task claim.

Expected:

- ProjectWork retains the original `host_id`
- recovery waits for that Host
- the claimed Task is not silently moved to another checkout

## 9. Remote Desktop RPC

For a Desktop-hosted Project OS project verify:

- Desktop Runner advertises `projectctl`
- Controller sends `PROJECT_OPERATION_REQUEST`
- Runner performs only a recognized operation
- `PROJECT_OPERATION_RESULT` returns structured output

Trying an unknown operation must fail.

## 10. Generic Git regression

Register a project with:

```text
adapter=generic_git
```

It must still run without a `.project-os` directory or `projectctl`.

## Automated coverage

CI covers:

- adapter config loading
- Generic Git snapshot instruction
- Project OS status/next/context/claim sequence
- persisted ProjectWork binding
- repeated prepare without duplicate next/claim
- claim crash reconciliation
- no eligible work
- successful Agent → submit lifecycle
- submit failure
- restart finalization recovery
- Project OS adapter aliases
- remote project RPC result/error
- unsafe operation/task identifier rejection
- project registry writer
- R0-R4 regression suite

A real Telegram + real Codex + real Project OS + actual Lightsail/Desktop smoke test still requires those runtime Hosts and credentials.
