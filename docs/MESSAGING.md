# Messaging

R0 implements Telegram through the `MessagingProvider` boundary.

Supported commands:

```text
/projects
/status
/run <project> [--host <host-id>]
/jobs
/job <job-id>
/stop
```

A small deterministic natural-language parser recognizes a registered project plus run/continue intent. If text cannot be mapped safely, it is rejected and the user is asked to use a slash command.

Completion/failure notifications are sent back to the user that requested the job.

R2 will add progress throttling and steering. R3 will add inline-button approvals.
