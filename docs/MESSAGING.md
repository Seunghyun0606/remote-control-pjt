# Messaging

Telegram is the first provider and is isolated behind the messaging boundary.

Supported commands:

```text
/projects
/status
/hosts
/run <project> [--host <host-id>]
/jobs
/job <job-id>
/stop
```

Examples:

```text
/run dailytown --host lightsail-main
/run dailytown --host desktop-main
DailyTown desktop에서 다음 작업 진행해
```

A small deterministic natural-language parser recognizes a registered project, run/continue intent and allowed host name. Unmapped text is rejected instead of being treated as shell input.

R2 will add progress throttling and steering. R3 will add inline-button approvals.
