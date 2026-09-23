# Security

## R3 controls

1. Telegram authorization uses numeric user IDs from an allowlist.
2. An Approval response is accepted only from the user ID that owns the original Job.
3. Messenger text never becomes a shell command.
4. Plain text steering is accepted only for a validated active Job.
5. While one Approval is pending, arbitrary text cannot bypass the Human Gate as steering.
6. Human decisions become Codex instructions; option keys are never executed as shell commands.
7. Project working directories come from server-side configuration, not Messenger input.
8. Codex uses direct subprocess argument execution, not shell interpolation.
9. Default Codex sandbox is `workspace-write`.
10. Controller never stores or forwards Codex/ChatGPT login credentials.
11. Desktop opens the WebSocket connection outbound; it has no inbound listener.
12. Controller/Runner transport requires a configured shared secret.
13. Controller HTTP binds to `127.0.0.1` by default.

## Approval data

The Approval Registry stores runtime metadata:

- Job/user association
- decision type and question
- options/details
- status and selected option
- timestamps

It does not store Codex credentials.

Telegram callback authorization has two layers:

```text
numeric allowlist
  ↓
Approval requested_by_user match
```

A callback copied from another user is therefore rejected.

## Human Gate trust boundary

The agent may request a gate, but the agent does not change runtime state directly. The Controller validates the event and performs the code-defined transition:

```text
RUNNING -> WAITING_HUMAN -> RUNNING
```

The Controller also prevents the gated turn from continuing unattended.

## Runner transport

Use a long random shared secret for Controller/Runner transport. Use TLS or a private network when the connection crosses an untrusted network.

The Desktop Runner connects outbound. Do not expose an inbound Desktop listener.

## Controller API

The HTTP API binds to loopback by default. It does not become safe for unrestricted Internet exposure merely because Telegram has an allowlist.

Use a private network/reverse proxy authentication if exposing it beyond the local host. Broader API authentication is outside R3.

## Codex authentication

Each execution Host owns its own Codex CLI authentication under that Host's local user account.

Never copy Codex authentication files, ChatGPT session credentials, or OpenAI access tokens into the Controller database or `.env`.

## Secrets

`.env` is gitignored. It may contain:

- Telegram bot credential
- Controller/Runner transport secret

Do not place production SSH keys or broad user-home credentials inside an agent workspace.
