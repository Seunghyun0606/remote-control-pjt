# Messaging

R6 supports multiple Messenger providers without coupling Controller Core to one vendor.

```text
Telegram ─┐
          ├─> ControllerService -> validated Job -> Runner
Slack ────┘
```

The same command parser and Job ownership rules are used for every provider.

## Commands

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

If there is exactly one eligible active Job and no Human Gate is pending, unmatched plain text can be treated as steering for that Job.

## Channel-specific notifications

JobManager keeps a notifier registry keyed by the Job's `requested_by_channel`.

This prevents one provider from overwriting another when Telegram and Slack are enabled together.

Later progress, recovery, Human Gate and final notifications return only through the provider that created the Job.

Notification delivery is best-effort with short retries. A transient Telegram/Slack network failure must not change the Job lifecycle state or replace the original Agent error. After the initial attempt, JobManager retries twice with short backoff. If all attempts fail, it logs the error and appends a `NOTIFICATION_FAILED` event containing the channel, notification type, attempt count and delivery error.

Unexpected background Job task exceptions are explicitly consumed and logged by the task completion callback so asyncio does not emit an unobserved `Task exception was never retrieved` warning.

When ProjectWork has a task id, notification headers include it:

```text
[project / TASK-043 / JOB-...]
```

## Telegram

Telegram uses polling and a numeric user-ID allowlist.

On startup the provider registers the supported command menu with Telegram using `setMyCommands`.

Private Project Topics are supported when the bot has Telegram private topic mode enabled.

```text
Bot
├─ Project A Topic
├─ Project B Topic
└─ Project C Topic
```

`/start` and `/sync` reconcile `config/projects.yaml` with persisted Telegram topic mappings.

The Controller stores:

```text
telegram_project_topics
  user_id + project_id -> chat_id + message_thread_id

telegram_message_bindings
  chat_id + message_id -> project_id + job_id
```

Inside a mapped Project Topic:

- `/run` starts that Project without repeating the project id.
- `/status`, `/jobs`, pause/resume/stop are scoped to that Project.
- Plain text starts a Job when no Project Job is active.
- Plain text steers the single eligible Job when exactly one is active.
- Multiple eligible Jobs produce an inline Job chooser.
- Replying to a bot Job message steers the Job bound to that exact Telegram message.

Asynchronous progress, recovery, approval and final notifications are routed back to the Project Topic when a mapping exists. If topic mode is unavailable, Telegram falls back to the private chat without a thread.

Human Gate replies use inline keyboard callbacks. Every callback passes both:

- Telegram allowlist validation
- Approval owner validation

## Slack

Slack uses Bolt for Python in Socket Mode.

The Controller creates an outbound WebSocket connection to Slack, so Slack event delivery does not require opening a public inbound webhook port.

R6 Slack MVP is DM-first.

Recommended Slack app configuration:

1. Enable Socket Mode.
2. Create an App-Level Token with `connections:write`.
3. Add Bot scopes:
   - `chat:write`
   - `im:history`
   - `im:write`
4. Subscribe to the Bot event `message.im`.
5. Enable the App Home Messages tab if users will message the app there.
6. Put allowed Slack user IDs in `SLACK_ALLOWED_USER_IDS`.

Environment:

```dotenv
REMOTE_CONTROL_SLACK_ENABLED=true
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U12345678,U87654321
```

Immediate command responses are sent to the conversation where the command arrived. Later Job notifications are sent by DM to the originating Slack user.

Slack Human Gate messages use Block Kit buttons for options, Details and Reject.

## Feedback policy

Low-level Codex events are not streamed directly.

- Progress feedback is throttled.
- Important failures are immediate.
- Human-required feedback is immediate.
- Final completion/failure is immediate.
- Host/quota recovery changes are explicit.

Default progress limit is one progress delivery per 300 seconds per Job.

## Future providers

The Controller Core remains compatible with additional providers such as Discord, OpenClaw/Hermes gateways or another Web messaging surface by implementing the MessagingProvider boundary.


## Runtime commands

Additional runtime-oriented commands:

```text
/retry <failed-job-id>
/sessions
/session <session-id>
/doctor
```

`/retry` creates a new Job instead of mutating the FAILED history. `/doctor` reports Host, executable resolution, runtime paths, and local project path readiness.

On Windows, npm-installed `codex.cmd` / `codex.ps1` shims are supported by the runtime launcher.
