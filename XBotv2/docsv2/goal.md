# Goal Plugin

`builtin_plugins/goal` is the session's persistent objective state machine. It
keeps the reason for ongoing work separate from the concrete steps tracked by
TodoList.

## State

A session has at most one Goal record:

| Field | Meaning |
|---|---|
| `objective` | Required objective, limited to 2,000 characters. |
| `status` | `active`, `complete`, or `blocked`. |
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
`block` require a final `summary`. Only `create` accepts `token_budget`.

Goal is an internal session-state tool and is allowed by the runtime baseline.
An explicit deny rule still takes precedence.

## Command

The deterministic server command delegates to the same state machine:

```text
/goal                                      # inspect
/goal <objective>                          # create
/goal create [--token-budget N] <objective>
/goal update <objective>
/goal complete <execution-summary>
/goal block <blocking-summary>
/goal resume
/goal clear
```

The command does not ask the model to interpret or invoke a tool. Mutations are
rejected while a model turn is active.

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
items. It also does not start background turns; persistence and explicit resume
provide continuity without a second scheduler.
