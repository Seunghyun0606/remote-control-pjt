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

When ProjectWork has a task id, notification headers include it:

```text
[project / TASK-043 / JOB-...]
```

## Telegram

Telegram uses polling and a numeric user-ID allowlist.

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
