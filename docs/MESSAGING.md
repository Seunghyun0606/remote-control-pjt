# Messaging

Telegram remains the first messaging provider.

Commands:

```text
/projects
/status
/hosts
/run <project> [--host <host-id>]
/jobs
/job <job-id>
/pause [job-id]
/resume [job-id]
/steer [--job <job-id>] <instruction>
/send [--job <job-id>] <instruction>
/stop [job-id]
```

If there is exactly one RUNNING Job and no Human Gate is pending, otherwise-unmatched plain text is treated as steering for that Job.

## Human Gate

When a Job enters `WAITING_HUMAN`, Telegram sends an inline keyboard with:

- one button per Approval option
- `Details`
- `Reject`

The callback always passes through the numeric Telegram user allowlist and the Approval owner check.

If exactly one Approval is pending, the user may send its short option key as text:

```text
B
```

Other non-command text is blocked while that Human Gate is pending instead of being converted to steering.

## Feedback policy

Do not stream every low-level Codex event to Telegram.

- Progress feedback is throttled.
- Command failures can be sent as important feedback.
- Human-required feedback is immediate.
- Final completion/failure is immediate.

Default progress limit is one progress delivery per 300 seconds per Job.
