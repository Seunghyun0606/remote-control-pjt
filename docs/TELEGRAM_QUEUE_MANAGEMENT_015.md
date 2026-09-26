# Telegram Queue Management — 0.15.0

Remote Control 0.15.0 adds a management surface for the durable working-tree queue introduced in 0.14.0.

## Command

```text
/queue
```

In a Project Topic this shows the current non-terminal Job(s) for that project and the user's `WAITING_LEASE` Jobs in execution order.

Outside a Project Topic it shows the same information across the user's projects.

## Telegram controls

Each queued Job receives inline controls:

- `⬆`: move the Job one position earlier in the same lease lane.
- `⬇`: move the Job one position later in the same lease lane.
- `✕`: cancel the queued Job.
- `🔄 새로고침`: refresh the current/queue view.

A lease lane is defined by the same identity used for execution exclusivity: host plus canonical working directory. Jobs targeting independent checkouts can still execute in parallel and are not forced into one artificial global serial queue.

## Durable ordering

Schema v6 adds:

```text
recovery.queue_position INTEGER NULL
```

Only `WAITING_LEASE` recovery records carry a queue position. Allocation is serialized by the Controller, and order changes swap durable positions in one DB transaction.

When a queued Job successfully obtains the lease, `queue_position` is cleared. Cancelled/terminal Jobs remove their recovery record through the existing lifecycle cleanup.

Controller restart preserves existing positions. If an old `WAITING_LEASE` row is missing a position, startup reconstructs one after the currently known queue.

## Permissions and fairness

A user may only reorder a Job they own. Reordering is limited to the Job's actual lease lane. If the immediately adjacent lane entry belongs to another user, Remote Control refuses to move through it rather than silently changing another user's priority.

## Compatibility

- Package: 0.15.0
- Runner Protocol: v3
- Database schema: v6
- Migration: v5 → v6 adds nullable `recovery.queue_position`
