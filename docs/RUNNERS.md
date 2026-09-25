# Runners

## AgentRunner

Operations:

- `start`
- `resume`
- `steer`
- `cancel`

Codex instructions are passed through stdin and process arguments are constructed directly.

For `codex exec resume`, exec-level options such as `--sandbox` and `--cd` are placed before the `resume` subcommand. This matches the Codex CLI parser: resume-specific arguments only contain session selection and prompt fields.

Codex JSONL stdout can contain large single-line events (for example command/tool output). The Runner launches Codex with a 16 MiB asyncio stream limit instead of Python's small default line limit so a large JSONL event does not terminate the Job with `Separator is not found, and chunk exceed the limit`.

## ProjectOperationExecutor

R5 adds a separate short-lived operation boundary for repository/Project OS metadata.

Local operations run on the Controller Host. Remote operations use the authenticated Runner WebSocket.

This boundary is deliberately separate from AgentRunner because:

- Project state selection must be deterministic
- Controller state transitions should not depend on an LLM
- Project OS canonical mutations must pass through `projectctl`
- remote project metadata access must not open arbitrary shell execution

## Desktop requirements

For a generic Git project:

- Git
- Codex CLI

For a Project OS project:

- Git
- Codex CLI
- `projectctl`
- the corresponding Project OS-enabled checkout

Default Runner capabilities include:

```text
codex,git,projectctl
```

Additional capabilities such as Android, GUI or browser may be configured.

## Reconnect behavior

The Runner continues active Codex execution if only the Controller WebSocket is lost.

R4 active execution adoption is unchanged.

R5 Project Operations are short requests. A connection loss fails the request; JobManager recovery decides whether to wait for the pinned Host and retry.

## Credentials

Codex credentials remain local to each execution Host. Project Operation payloads do not carry Codex auth.
