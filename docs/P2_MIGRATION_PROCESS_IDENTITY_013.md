# P2 Migration & Process Identity Hardening — 0.13.0

Remote Control 0.13.0 closes the remaining P2 integrity gaps in database migration history and persisted OS-process ownership.

## 1. Immutable schema history

Before 0.13.0, migration v1 used the live ORM metadata:

```python
Base.metadata.create_all(...)
```

That made migration v1 mutable. If a future model or column were added to the ORM, a brand-new database could receive that future object during migration v1 while an existing database would receive it only through its numbered migration.

0.13.0 freezes the original v1 schema in:

```text
src/remote_control/storage/schema_v1.py
```

The migration path is now:

```text
v1 immutable baseline
→ v2 execution leases
→ v3 Runner identity
→ v4 remote execution ownership
→ v5 persisted local process identity
```

Future schema changes must add a new numbered migration. Historical migration snapshots must not be edited to include later schema objects.

### Verification

Regression coverage creates both:

- a fresh database starting at version 0;
- an upgrade database initialized explicitly at frozen v1.

The test compares their final table columns and indexes structurally after both reach the current schema.

## 2. Schema v5

Schema v5 adds nullable columns to `jobs`:

```text
process_executable
process_start_token
```

They are nullable for upgrade compatibility, but an active persisted local process without them is not considered safe to terminate automatically.

No protocol change is required. Runner Protocol remains v3.

## 3. Strong process ownership

A numeric PID can be reused by the operating system. Therefore:

```text
PID + --cd
```

is no longer sufficient proof that a live process is the Codex process previously owned by Remote Control.

For every newly spawned local Codex process, Remote Control captures:

```text
pid
actual OS executable
OS process start/creation token
expected --cd working directory
```

A persisted process tree is terminated only when all four match.

If any identity field is missing, unreadable, or different, recovery fails closed and does not send a kill signal.

## 4. Platform identity

### Linux

Remote Control reads:

```text
/proc/<pid>/exe
/proc/<pid>/stat field 22 (starttime)
```

The start token is based on the process start clock tick since boot, so a later process that reuses the same numeric PID does not match.

### Windows

Remote Control queries `Win32_Process` and records:

```text
ExecutablePath
CreationDate
```

`CreationDate` is converted to a stable UTC tick token.

This also works when Codex is launched through npm wrappers: the owned root process may be `cmd.exe` or PowerShell, while the persisted command line still has to contain the expected Codex `--cd` argument.

## 5. Local Controller recovery

For a non-terminal local Job after an unclean restart:

```text
PID exists?
  ↓
persisted executable/start token exist?
  ↓
live executable matches?
  ↓
live start token matches?
  ↓
command line contains expected --cd?
  ↓
terminate process tree
  ↓
confirm stopped
  ↓
clear PID + identity
  ↓
continue normal Job recovery
```

Any failed proof stops Controller startup rather than risking termination of an unrelated process.

## 6. Remote Runner journal

Runner journal `RUNNING` entries now persist:

```text
pid
process_executable
process_start_token
working_directory
```

A restarted Runner applies the same identity proof before terminating an orphan execution.

`COMPLETED` entries do not require process identity because there is no process to terminate; they remain durable until `JOB_RESULT_ACK`.

A legacy `RUNNING` entry created before 0.13.0 may not contain executable/start-token fields. Such an entry intentionally fails closed.

## 7. Upgrade procedure

Recommended upgrade:

1. Stop Controller and Remote Runners cleanly.
2. Confirm no Codex Job remains actively running.
3. Back up the Controller SQLite database and Runner journal files.
4. Upgrade Controller and all Runners to 0.13.0.
5. Start the Controller; schema v4 → v5 is automatic.
6. Start each Runner.
7. Run a short Job and verify normal completion/recovery metadata.

If upgrading after a crash with a legacy active PID but no process identity, do not delete safety state merely to bypass startup. Inspect and terminate the old process manually, then reconcile the persisted state.

## 8. Compatibility

```text
Package: 0.13.0
Runner Protocol: v3
Database schema: v5
```

The Controller and Remote Runners should still be upgraded together because Protocol v3 generation/ownership guarantees remain part of the safety model.
