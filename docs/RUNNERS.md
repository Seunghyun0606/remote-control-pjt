# Runners

## AgentRunner

Operations:

- `start`
- `resume`
- `steer`
- `cancel`

Codex instructions are passed through stdin and process arguments are constructed directly.

For `codex exec resume`, exec-level options such as `--sandbox` and `--cd` are placed before the `resume` subcommand. This matches the Codex CLI parser: resume-specific arguments only contain session selection and prompt fields.

Codex JSONL stdout can contain very large single-line events (for example command/tool output). The Runner reads stdout in 64 KiB chunks and performs newline framing itself instead of relying on asyncio's line-separator limit. One record is retained up to 32 MiB; larger records are drained to the next newline and surfaced as bounded `raw_output_truncated` events. This prevents `Separator is not found, and chunk exceed the limit` from terminating the Job while keeping memory bounded.


## Session resume failure policy

Remote Control does not treat every non-zero `codex exec resume` as a missing session.

A fresh Codex thread is created only for an explicitly unavailable saved session/thread/rollout or for `SESSION_IDENTITY_MISMATCH`. CLI syntax errors, launch errors, permission/filesystem failures and unknown Codex failures remain failures and preserve the original error instead of silently starting unrelated work.

The event ledger records `SESSION_RESUME_FAILED.fallback_allowed` before any fallback.

## Runner supervision

The Remote Runner is a long-lived supervisor. Heartbeat and receive loops are supervised as one connection generation: failure of either tears down that generation and reconnects. Unexpected protocol/handler exceptions are logged and reconnect rather than terminating `run_forever`.

Background Job-finalizer task exceptions are also consumed and logged so they do not become unobserved asyncio task failures.

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

A reconnect that replaces an existing WebSocket is connection-generation safe: cleanup from the old socket is ignored once a newer socket for the same `host_id` is active. The stale socket therefore cannot fail pending Jobs or mark the replacement Host connection offline.

Remote cancellation is acknowledgement-based. `JOB_CANCEL` only requests termination; the Controller keeps the Job in `CANCELLING` and retains the Project Session lock until the Runner returns a terminal `JOB_RESULT`. See [Cancellation and Runner Reconnect Safety](CANCELLATION_AND_RECONNECT.md).

R5 Project Operations are short requests. A connection loss fails the request; JobManager recovery decides whether to wait for the pinned Host and retry.

## Credentials

Codex credentials remain local to each execution Host. Project Operation payloads do not carry Codex auth.
