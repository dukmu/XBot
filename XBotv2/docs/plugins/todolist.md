# TodoList Plugin

`todolist` provides thread-scoped progress tracking through
one atomic, model-facing Tool. It uses the public Tool and thread storage APIs
and does not infer tasks from assistant prose.

## Tool Contract

`update_todos` replaces the complete desired checklist when its contents or
status actually changes:

```json
{
  "todos": [
    {"content": "Inspect current behavior", "status": "completed"},
    {"content": "Implement the fix", "status": "in_progress"},
    {"content": "Run verification", "status": "pending"}
  ]
}
```

Each item requires `content` and one status: `pending`, `in_progress`, or
`completed`. An unfinished non-empty list must contain exactly one
`in_progress` item. `todos: []` clears the list. The complete list is validated
before one persisted replacement; invalid input cannot partially modify state.
Items represent work, not a final answer, report, or summary. After the current
item is finished and checked, its completion and the next `in_progress` item
are submitted before that next work starts or the Agent replies. The final
all-completed update happens before the final reply and clears active state.

The Tool description defines when Todo tracking is useful and when it is not.
The plugin does not register per-item list, create, update, or remove Tools.

## Results And Completion

Every successful result includes a versioned `todo_snapshot` as structured Tool
data. Repeating the current list is a no-op and tells the model to continue the
work before calling the Tool again. When every supplied item is completed, the
stored and projected current snapshot is empty.
Clients receive this through the existing `tool_result` protocol event; Todo
does not add a plugin-specific wire event.

## Context And Persistence

Todo Tool calls and results follow the normal conversation path. In particular,
the result remains visible to the next model call so the model knows that its
update succeeded. The plugin does not rewrite provider context or repeatedly
inject the active list as a system message.

The plugin stores one strict `TodoSnapshot` through
`ctx.state.namespace("todolist")`. It never selects a filename or writes the
plugin-state directory directly. A changed list performs one atomic state
write, so thread resume observes the same checklist. The same structured
snapshot is persisted on the normal Tool-result message and drives WebUI/TUI
live and restored rendering; clients do not infer current state from Tool args.

Todo items track concrete work. They do not own the durable session objective;
that belongs to the separate Goal plugin.
