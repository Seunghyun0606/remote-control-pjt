# Human Gate

R3 adds explicit human decision points without moving project/product state into Remote Control.

## Runtime model

An Approval record contains:

```text
id
job_id
requested_by_user
approval_type
question
details
options
status
selected_option
response_text
expires_at
resolved_at
created_at
```

Status values:

- `PENDING`
- `RESOLVED`
- `REJECTED`
- `CANCELLED`
- `EXPIRED`

R3 persists `expires_at`, but automatic expiry requires the R4 Scheduler.

## Gate detection

A Runner can send a structured `HUMAN_GATE` event.

For Codex CLI non-interactive execution, Remote Control also appends a marker contract to the agent instruction:

```text
REMOTE_CONTROL_HUMAN_GATE
{"type":"architecture_change","question":"...","details":"...","options":[{"key":"A","label":"..."},{"key":"B","label":"..."}]}
```

The parser also accepts request-user-input-shaped structured events and normalizes them to the same model.

The marker is a Remote Control protocol, not a claim that every Codex CLI version emits this shape natively.

## State transition

```text
RUNNING
  ↓
WAITING_HUMAN
  ↓ human response
RUNNING
```

When the gate is created:

1. persist the Approval
2. log `HUMAN_GATE_CREATED`
3. transition the Job to `WAITING_HUMAN`
4. mark the Session `WAITING_HUMAN`
5. notify Telegram
6. stop the current non-interactive turn while preserving the session identifier

When the human responds:

1. validate the responder against the original Job user
2. resolve/reject the Approval
3. log `HUMAN_GATE_RESOLVED`
4. transition the Job to `RUNNING`
5. resume the same Host/session when available
6. send the human decision as the next agent instruction

If same-session resume fails, the existing R2 fallback reloads repository state and starts a new session.

## Telegram

An approval message contains option buttons plus:

- `Details`
- `Reject`

If exactly one approval is pending for the user, typing an option key such as `B` also resolves it.

While an Approval is pending, arbitrary non-command text is not treated as steering. This prevents an accidental message from bypassing a required decision.

## API

R3 exposes:

```text
GET  /approvals
GET  /approvals/{id}
POST /approvals/{id}/respond
```

The API is intended behind the Controller's existing loopback/private-network boundary. It should not be exposed as an unrestricted public endpoint.

## Scope boundary

R3 does not implement:

- scheduled approval expiry
- notification retries
- controller-restart reconciliation of active processes
- WAITING_HOST recovery
- WAITING_QUOTA recovery

Those require R4 recovery/scheduler work.
