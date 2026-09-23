# Security

## Control boundaries

1. Telegram and Slack authorization use provider-specific user-ID allowlists.
2. Approval responses are restricted to the original Job user.
3. Messenger text never becomes a shell command.
4. Plain text steering is accepted only for a validated active Job.
5. Human Gate decisions become Codex instructions, not shell commands.
6. Project working directories come only from the server-side Project Registry.
7. Codex is launched with direct subprocess arguments, not shell interpolation.
8. Project Operations use a fixed whitelist and direct subprocess argv.
9. Project OS canonical state is mutated only through `projectctl`.
10. Default Codex sandbox is `workspace-write`.
11. Controller never stores or forwards Codex/ChatGPT credentials.
12. Desktop opens the WebSocket connection outbound; it has no inbound listener.
13. Controller/Runner transport requires a shared secret.
14. Controller HTTP binds to `127.0.0.1` by default.

## Project Operation whitelist

Allowed operations:

- `git_snapshot`
- `project_os_status`
- `project_os_next`
- `project_os_context`
- `project_os_claim`
- `project_os_submit`

There is no generic `shell` Project Operation.

Task id, role and actor values used to construct `projectctl` argv are validated before execution.

Messenger input cannot supply a working directory. The working directory comes from the registered Project/Host mapping.

## Project OS trust boundary

Remote Control stores only runtime ProjectWork references. It does not directly edit:

- backlog YAML
- current-state YAML
- Project OS implementation handoff directories

The Project OS adapter invokes `projectctl`, which remains responsible for its own canonical transition rules.

Remote Job completion does not self-approve a Project OS task.

## Remote project operations

A Desktop Project Operation request is accepted only over the authenticated outbound Runner connection.

The Controller sends a typed operation name plus structured payload, not an arbitrary executable or shell command.

The Runner maps that operation to local fixed command construction.

## Recovery trust boundary

Runner/Codex reports do not directly mutate Job state. JobManager validates transitions.

A claimed Project OS work item is pinned to its original Host during recovery to avoid cross-checkout continuation.

## Secrets

`.env` may contain Telegram, Slack and Controller/Runner transport secrets. Slack bot/app tokens must never be committed or sent to execution Hosts.

Codex authentication and any Project OS environment-specific credentials remain local to each execution Host.


## Web dashboard boundary

The R6 `/ui` surface renders runtime state only. It does not expose run, steer, stop, approval or shell controls.

`/dashboard` and the existing HTTP API still contain operational information. The Controller remains loopback-bound by default. If browser access is exposed beyond the trusted host, put it behind an authenticated reverse proxy or private network.

## Slack boundary

Slack commands pass through the same ControllerService validation as Telegram. Slack text is never treated as a shell command.

Socket Mode authenticates the Slack app connection with the app-level token. User authorization is separate and uses `SLACK_ALLOWED_USER_IDS`. Human Gate responses still require Approval ownership.
