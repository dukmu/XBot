# Goal Plugin

`builtin_plugins/goal` is the session's persistent objective state machine. It
keeps the reason for ongoing work separate from the concrete steps tracked by
TodoList.

## State

A session has at most one Goal record:

| Field | Meaning |
|---|---|
| `objective` | Required objective, limited to 2,000 characters. |
| `status` | `active`, `paused`, `complete`, or `blocked`. |
| `summary` | Completion or blocking summary, retained across resume. |
| `token_budget` | Optional positive budget supplied when the Goal is set. |

Token-budget accounting is not implemented yet. The value is persisted metadata,
not a claim that execution will stop at the declared limit.

Completion and blocking retain the Goal and summary until the human clears or
replaces it. Resume changes a terminal or paused Goal back to active.

## Human Command

`/goal` is a human-facing server command with compact, task-oriented syntax:

```text
/goal
/goal Stabilize the C/S API
/goal --token-budget 8000 Stabilize the C/S API
/goal pause
/goal resume
/goal complete Implementation, tests, and docs are complete
/goal block Waiting for human review
/goal clear
```

The command handler belongs to the plugin and calls the same private state
transitions as its Tools. It does not construct a Tool call, enter Tool
permissions, or append a Tool message to model history. Setting or resuming an
active Goal schedules its next mailbox turn.

## Agent Tools

The model receives structured Tools suited to JSON-schema invocation:

- `create_goal(objective, token_budget?)`
- `get_goal()`
- `update_goal(status, summary)`, where status is `complete` or `blocked`

Pause, resume, clear, and objective replacement remain human controls. The
plugin returns `HookDecision.ALLOW` for its three basic state Tools, avoiding an
`ask` prompt. An explicit core permission denial still wins.

After `update_goal`, the normal Agent loop continues so the model can summarize
the result to the human. The Tool result contains the terminal state and summary
needed for that final model call.

The Agent does not create a Goal merely because work is complex or uses Todos.
Before a terminal update it compares the complete objective with observed
evidence. `complete` requires every outcome and its verification; `blocked`
requires an exact external condition that still prevents progress after
reasonable attempts. A started background task, intent, or confidence is not
completion evidence.

## Continuation And Persistence

Every successful transition persists immediately through `PluginStore`. Goal
does not inject state during each context build. When an active Goal reaches an
idle session, its runtime-only mailbox wake causes `ON_TURN_START` to build one
non-persisted Goal snapshot for that turn. ReAct iterations reuse that stable
snapshot. Terminal and paused Goals remain available to the command and Tools,
but are not injected into unrelated model calls.

At `ON_TURN_END`, an active Goal places at most one wake-only `general` message
in the Core mailbox; the objective remains authoritative in `PluginStore` and is
not duplicated in the mailbox payload. Delivery clears the pending marker
before the next turn. ESC pauses the Goal and schedules no successor. `/goal
resume` activates it and schedules continuation again. Mailbox entries and
turn snapshots are not restored by session resume.

Goal does not infer objectives from ordinary conversation and does not create
TodoList items.
