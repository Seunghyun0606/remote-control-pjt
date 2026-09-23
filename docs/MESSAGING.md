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

If there is exactly one RUNNING Job, otherwise-unmatched plain text is treated as steering for that Job.

Example:

```text
UI는 건드리지 말고 backend만 수정해
```

This is an agent instruction, not a shell command. If there is no matching active Job, it is rejected.

## Feedback policy

Do not stream every low-level Codex event to Telegram.

- Progress feedback is throttled.
- Command failures can be sent as important feedback.
- Final completion/failure is always sent.
- Human-required feedback is implemented in R3.

Default progress limit is one progress delivery per 300 seconds per Job.
