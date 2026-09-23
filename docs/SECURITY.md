# Security

## R1 controls

1. Telegram authorization uses numeric user IDs from an allowlist.
2. Messenger text is parsed into deterministic intents and never becomes a shell command.
3. Project working directories come from server-side configuration.
4. Codex uses direct subprocess argument execution, not shell interpolation.
5. Default Codex sandbox is `workspace-write`.
6. Controller never stores or forwards Codex/ChatGPT login credentials.
7. Desktop opens the WebSocket connection outbound; it has no inbound listener.
8. Controller/Runner transport requires a configured shared secret.
9. Controller HTTP binds to `127.0.0.1` by default.

Use TLS or a private network when the Runner connection crosses an untrusted network.

Each execution Host owns its own Codex CLI authentication. Never copy Codex authentication files or ChatGPT session credentials into the Controller database.

`.env` is gitignored and may contain the Telegram bot credential and Controller/Runner transport secret.
