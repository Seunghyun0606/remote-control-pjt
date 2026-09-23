# R3 Manual Smoke Test

This checklist requires a real Telegram bot and a Codex-authenticated execution Host.

## 1. Start Controller and Runner

Start the Controller and verify:

```text
/hosts
```

Expected: the target Host is `ONLINE`.

For Desktop, start `remote-runner start` and verify it connects outbound.

## 2. Start a controlled Human Gate task

Use a disposable test project. Start a Job and give it a task that intentionally requires a harmless architectural choice, for example choosing between two temporary fixture formats.

Expected:

- Job starts as usual.
- Codex emits the Remote Control Human Gate marker/event before making the gated change.
- Job becomes `WAITING_HUMAN`.
- `GET /approvals` contains one `PENDING` Approval.
- `/status` shows `WAITING_HUMAN`.

Do not use production/destructive data for this smoke test.

## 3. Telegram buttons

Expected message:

```text
⚠ Human Gate
...
[A ...] [B ...]
[Details] [Reject]
```

Tap `Details`.

Expected: question, details and all option descriptions are sent without resolving the Approval.

Tap an option.

Expected:

- original Approval message becomes a resolved confirmation
- Approval status = `RESOLVED`
- selected option is persisted
- Event Ledger contains `HUMAN_GATE_RESOLVED`
- Job returns to `RUNNING`
- same Host is used
- same Codex external session id is attempted first
- work completes according to the selected option

## 4. Text response

Trigger another gate, then send:

```text
B
```

Expected: if exactly one Approval is pending, it resolves identically to the inline button.

Send arbitrary text instead while the gate is pending.

Expected: it is rejected as a Human Gate response and is not queued as steering.

## 5. Reject path

Trigger another gate and tap `Reject`.

Expected:

- Approval status = `REJECTED`
- Job returns to `RUNNING`
- Codex receives an instruction not to perform the gated change
- Codex either chooses a safe alternative or reports the blocker

## 6. Authorization

Using a Telegram user not in the allowlist, attempt to interact with the bot.

Expected: request rejected.

For a test with two authorized users, attempt to use user B on an Approval created for user A.

Expected: Approval owner check rejects the response.

## 7. Desktop Human Gate

Run the same disposable scenario on `desktop-main`.

Expected:

```text
Desktop Codex
  ↓
HUMAN_GATE WebSocket event
  ↓
Controller ApprovalRegistry
  ↓
Telegram
  ↓
human response
  ↓
JOB_RESUME on desktop-main
```

Desktop remains outbound-only and its Codex authentication remains local.

## 8. Stop while waiting

Trigger a gate, then run:

```text
/stop <job-id>
```

Expected:

- Job = `CANCELLED`
- pending Approval = `CANCELLED`
- later button clicks cannot resume the Job

## Automated coverage

CI covers:

- Human Gate marker parsing
- request-user-input-shaped event parsing
- Approval create/resolve/reject
- WAITING_HUMAN transition
- same-session response resume
- wrong-user response rejection
- Telegram callback payload construction
- remote `HUMAN_GATE` forwarding
- all R0/R1/R2 regression tests

Automatic Approval expiry and restart reconciliation are R4.
