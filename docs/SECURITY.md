# Security

## R2 controls

1. Telegram authorization uses numeric user IDs from an allowlist.
2. Messenger text never becomes a shell command.
3. Plain text steering is accepted only for a validated active Job and becomes a Codex agent instruction.
4. Project working directories come from server-side configuration, not Messenger input.
5. Codex uses direct subprocess argument execution, not shell interpolation.
6. Default Codex sandbox is `workspace-write`.
7. Controller never stores or forwards Codex/ChatGPT login credentials.
8. Desktop opens the WebSocket connection outbound; it has no inbound listener.
9. Controller/Runner transport requires a configured shared secret.
10. Controller HTTP binds to `127.0.0.1` by default.

## Session data

The Session Registry stores runtime metadata such as:

- internal Session ID
- Job / Project / Host IDs
- external Codex thread/session ID
- status and timestamps

It does **not** store Codex authentication files, ChatGPT session credentials, or OpenAI access tokens.

An external Codex session ID is a runtime reference used to ask the same Host to resume its local Codex session. The Controller does not use that ID as an authentication credential.

## Steering

A Messenger steering message follows:

```text
Messenger
  ↓
Controller
  ↓
validated active Job
  ↓
Session Registry
  ↓
Runner
  ↓
Codex resume/steer
```

There is no Messenger → subprocess shell path.

R2 applies steering at a safe turn boundary rather than force-killing a Codex process that may be writing files.

## Runner transport

Use a long random shared secret for Controller/Runner transport. Use TLS or a private network when the connection crosses an untrusted network.

The Desktop Runner connects outbound. Do not expose an inbound Desktop listener.

## Codex authentication

Each execution Host owns its own Codex CLI authentication under that Host's local user account.

Never copy Codex authentication files, ChatGPT session credentials, or OpenAI access tokens into the Controller database or `.env`.

## Secrets

`.env` is gitignored. It may contain:

- Telegram bot credential
- Controller/Runner transport secret

Do not place production SSH keys or broad user-home credentials inside an agent workspace.
