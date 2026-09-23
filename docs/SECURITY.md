# Security

## Control boundaries

1. Telegram authorization uses numeric user IDs from an allowlist.
2. Approval responses are restricted to the original Job user.
3. Messenger text never becomes a shell command.
4. Plain text steering is accepted only for a validated active Job.
5. Human Gate decisions become Codex instructions, not shell commands.
6. Project working directories come only from the server-side Project Registry.
7. Codex is launched with direct subprocess arguments, not shell interpolation.
8. Default Codex sandbox is `workspace-write`.
9. Controller never stores or forwards Codex/ChatGPT credentials.
10. Desktop opens the WebSocket connection outbound; it has no inbound listener.
11. Controller/Runner transport requires a shared secret.
12. Controller HTTP binds to `127.0.0.1` by default.

## Recovery trust boundary

Recovery is Controller-owned code state.

A Runner or Codex process may report:

- quota
- disconnect/error
- current execution id
- session id
- Human Gate

Those reports do not directly mutate the state machine. JobManager validates the current state and performs allowed transitions.

## Persisted recovery metadata

The Recovery table stores runtime references such as:

- Job id
- Host/retry kind
- retry timestamp
- remote execution id
- Codex session continuation instruction

It does not contain Codex login files, ChatGPT credentials, SSH private keys, or OpenAI tokens.

## Remote execution adoption

`execution_id` is a correlation identifier, not authentication.

A remote Runner can report/adopt jobs only after establishing the authenticated Controller WebSocket using the configured Runner transport secret.

Desktop remains outbound-only.

## Quota recovery

Quota retry never weakens the configured Codex sandbox or approval policy. A retry starts/resumes through the same AgentRunner boundary.

## Human Gate expiry

Expired Human Gates fail closed: the Approval becomes `EXPIRED` and the Job becomes `FAILED`. The system does not infer a default architecture/destructive choice.

## Controller API

The HTTP API binds to loopback by default. Telegram authorization does not protect an independently exposed HTTP port.

Use a private network or authenticated reverse proxy before exposing the API outside the trusted host.

## Secrets

`.env` is gitignored. It may contain:

- Telegram bot credential
- Controller/Runner transport secret

Codex authentication belongs to each execution Host's local user account.
