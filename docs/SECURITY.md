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
15. Non-loopback HTTP bind requires `CONTROLLER_API_TOKEN`.
16. When configured, the Control API token protects REST and Web UI HTTP endpoints except `/health`.

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


## Control API authentication

The Runner WebSocket and the HTTP Control API use separate secrets:

```dotenv
CONTROLLER_RUNNER_TOKEN=<runner-shared-secret>
CONTROLLER_API_TOKEN=<http-api-secret>
```

If `REMOTE_CONTROL_API_HOST` is non-loopback, Controller startup fails unless `CONTROLLER_API_TOKEN` is configured.

When configured, HTTP requests except `/health` must provide either:

```http
Authorization: Bearer <token>
```

or:

```http
X-Remote-Control-Token: <token>
```

Do not reuse Telegram, Slack or Codex credentials as the Control API token.


### Control API principal

HTTP authentication and ownership identity are separate concepts.

```dotenv
CONTROLLER_API_TOKEN=<http-api-secret>
CONTROLLER_API_PRINCIPAL=api:controller
```

The client cannot choose Job or Approval ownership through request JSON. API-created Jobs always use the configured server-side principal, and API Human Gate responses are accepted only for approvals owned by that same principal. Request fields such as `requested_by` and `user_id` are rejected rather than trusted.

The Control API token is still operationally privileged for endpoints such as pause/resume/cancel. `CONTROLLER_API_PRINCIPAL` prevents identity impersonation; it is not a replacement for endpoint authorization.

## Web dashboard boundary

The R6 `/ui` surface renders runtime state only. It does not expose run, steer, stop, approval or shell controls.

`/dashboard` and the existing HTTP API still contain operational information. The Controller remains loopback-bound by default. If `CONTROLLER_API_TOKEN` is configured, `/ui` and `/dashboard` are protected by the same HTTP authentication boundary. For browser-facing remote access, prefer an authenticated reverse proxy/private network rather than exposing the Controller directly.

## Process and working-tree boundary

A Project Session controls conversational context; it is not the repository concurrency lock. Remote Control 0.11.0 uses a separate DB-backed execution lease keyed by execution host and canonical working directory, so different users or messaging channels cannot write the same checkout concurrently.

Persisted PIDs are not trusted by number alone. Crash recovery requires the live OS executable and process start/creation token to match the durable identity captured at spawn, and also verifies the process command contains the expected Codex working-directory argument before terminating the process tree. PID reuse, missing identity, or any mismatch fails closed without sending a kill signal.

Runner Protocol v3 prevents older peers from bypassing stable Runner instance identity, durable result acknowledgement, execution ownership, and execution-journal safety.

## Telegram selection token boundary

When a Telegram Project has multiple steerable Jobs, the temporary `jobselect` token is owned by the user who created it. Ownership is checked before the token is consumed.

Tokens expire after `REMOTE_CONTROL_TELEGRAM_SELECTION_TTL_SECONDS` (default 300 seconds) and are removed on expiry. This prevents stale inline buttons from remaining actionable indefinitely.

## Slack boundary

Slack commands pass through the same ControllerService validation as Telegram. Slack text is never treated as a shell command.

Socket Mode authenticates the Slack app connection with the app-level token. User authorization is separate and uses `SLACK_ALLOWED_USER_IDS`. Human Gate responses still require Approval ownership.
