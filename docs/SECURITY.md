# Security

## R0 controls

1. Telegram authorization is based on numeric user IDs from an allowlist.
2. Telegram input is parsed into a small set of deterministic intents.
3. Natural language is never converted into a shell command.
4. Project paths come from the server-side project registry, not from Messenger input.
5. Codex is launched with `create_subprocess_exec`; no shell interpolation is used.
6. The default Codex sandbox is `workspace-write`.
7. The Controller never stores or forwards Codex/ChatGPT login tokens.
8. The API binds to `127.0.0.1` by default.

## Host authentication

Each execution host owns its own Codex CLI authentication under that host's local user account.

Do not copy `~/.codex/auth.json`, ChatGPT session tokens or access tokens into the Controller database or `.env`.

## Secrets

`.env` may contain the Telegram bot token and future Controller transport secrets. `.env` is gitignored.

## R1+

Remote runners must connect outbound to the Controller and authenticate the transport. A Desktop inbound listener is not part of the design.
