# Startup Preflight — P1 Closure

This change closes the remaining P1 startup-ordering issue identified after the process-safety review.

## Problem

Previously the Controller performed database initialization, local-host registration, execution/session reconciliation and recovery state mutation before validating some messaging configuration.

A missing Telegram token or incomplete Slack configuration could therefore produce this sequence:

```text
Controller start
→ DB open / schema migration
→ host registration
→ Job / Project Session / execution lease reconciliation
→ configuration error discovered
→ startup abort
```

The process never became available to the user, but persistent runtime state could already have changed.

## Invariant

All static configuration errors must fail before the Controller performs persistent runtime mutation.

The startup order is now:

```text
Settings load
→ controller configuration preflight
→ Project registry load
→ executable/project startup preflight
→ DB init / migrations
→ host registration
→ Job / session / lease reconciliation
→ provider construction
→ scheduler / providers / API start
```

## Configuration preflight

The Controller now validates these together:

- `CONTROLLER_RUNNER_TOKEN`
- non-loopback Control API requires `CONTROLLER_API_TOKEN`
- Telegram token when Telegram is enabled
- non-empty Telegram allowlist when Telegram is enabled
- Telegram allowlist integer syntax
- Slack bot/app tokens when Slack is enabled
- non-empty Slack allowlist when Slack is enabled

`--no-telegram` intentionally disables Telegram token and allowlist requirements.

Failures are aggregated under:

```text
controller configuration preflight failed:
- ...
```

This allows operators to fix all static configuration errors in one pass.

## Regression guarantee

The test suite verifies that an invalid messaging configuration aborts before the configured SQLite database file is created.

This is deliberately stronger than only checking that `reconcile_startup()` was not called: no schema migration or other DB-side startup mutation may happen first.

## Relation to P0

Two items originally classified as P1 were closed during the P0 process-safety work because they became part of the process durability contract:

- Runner completed results are persisted until `JOB_RESULT_ACK`.
- an unknown `JOB_CANCEL` target returns `cancel_unconfirmed` and cannot be treated as confirmed cancellation.

Therefore startup fail-fast ordering was the remaining P1 item after P0 closure.
