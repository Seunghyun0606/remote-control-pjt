# Human Gate

R3 introduced explicit human decision points. R4 adds automatic expiry handling.

## Runtime model

Approval status values:

- `PENDING`
- `RESOLVED`
- `REJECTED`
- `CANCELLED`
- `EXPIRED`

## State transition

Normal response:

```text
RUNNING
  ↓
WAITING_HUMAN
  ↓ human response
RUNNING
```

Expired response window:

```text
WAITING_HUMAN
  ↓ expires_at
EXPIRED Approval
  +
FAILED Job
```

This is a fail-closed policy. The system never chooses an architecture/destructive option because the human did not respond.

## Gate detection

A Runner can send structured `HUMAN_GATE`.

For non-interactive Codex execution, Remote Control also appends the `REMOTE_CONTROL_HUMAN_GATE` marker contract to the instruction.

Both normalize to the same Approval Registry.

## Host loss while waiting

If the human responds while the originally assigned Host is offline, the decision remains persisted and the Job moves to `WAITING_HOST`.

When the Host becomes available, the decision is delivered through the normal session/repository resume path.

## Telegram

Approval messages contain:

- one button per option
- `Details`
- `Reject`

If exactly one Approval is pending, a short option key such as `B` can also resolve it.

Arbitrary text is not converted to steering while an Approval is pending.

## API

```text
GET  /approvals
GET  /approvals/{id}
POST /approvals/{id}/respond
```
