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
| `token_budget` | Optional positive budget supplied when the Goal is created. |

`token_budget` is currently persisted and returned as declared metadata. Usage
and remaining-budget enforcement require provider usage accounting and are not
implemented by this plugin yet.

Completion does not delete the Goal. The objective and execution summary remain
available until `clear` or replacement by a new Goal. `resume` changes a
complete or blocked Goal back to active without discarding its prior summary.

## Tool

The model sees one `goal` tool instead of separate lifecycle tools. Its `action`
is one of `get`, `create`, `update`, `complete`, `block`, `resume`, or `clear`.
`create` requires `objective` and may set an initial progress `summary`.
`update` changes `objective`, progress `summary`, or both. `complete` and
`block` require a final `summary`. `pause` stops automatic continuation without
discarding state. Only `create` accepts `token_budget`.

Goal is an internal session-state tool and is allowed by the runtime baseline.
An explicit deny rule still takes precedence.

## Command Discovery

The registered Tool is exposed as `/goal` by the shared ToolRegistry command
inventory. Goal does not register a second command handler or duplicate its
schema in the protocol layer.

## Context And Persistence

Every successful transition is immediately persisted through `PluginStore`.
The current Goal is rebuilt as a non-persisted `ContextComponent` during
`AFTER_CONTEXT_COMPONENTS_BUILD`.

Active context tells the model to persist until the objective is genuinely
finished and to include a concise execution summary when completing or
blocking. Complete context remains visible so the provider can give the human a
final summary after reading the tool result, while explicitly prohibiting it
from restarting the finished work. This avoids the previous failure where
completion removed the context during the same tool loop and the provider
restarted the original request.

Goal does not infer objectives from ordinary conversation or create TodoList
items. While active, `ON_TURN_END` places one `general` continuation in the Core
mailbox. Delivery clears the pending marker before the next turn, so one turn
can schedule at most one successor. ESC records `paused` and schedules no
successor; `/goal resume` makes the Goal active and starts continuation again.
Mailbox entries are runtime-only and are not recreated by session resume.
