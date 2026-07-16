# TodoList Plugin

`builtin_plugins/todolist` provides session-scoped progress tracking through
one atomic, model-facing Tool. It uses only the public Tool, Hook, and
`PluginStore` APIs and does not infer tasks from assistant prose.

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

The Tool description defines when Todo tracking is useful and when it is not.
The plugin does not register per-item list, create, update, or remove Tools.

## Results And Completion

Every result includes `todos` and `cleared` as structured Tool data. Repeating
the current list is a no-op and tells the model to continue the work before
calling the Tool again. When every supplied item is completed, the result
carries the final completed list and the active stored checklist is cleared.
Clients receive this through the existing `tool_result` protocol event; Todo
does not add a plugin-specific wire event.

## Context And Persistence

Todo Tool calls and results follow the normal conversation path. In particular,
the result remains visible to the next model call so the model knows that its
update succeeded. The plugin does not rewrite provider context or repeatedly
inject the active list as a system message.

The plugin stores only the ordered active item list. Old ID-based state is read
without exposing or continuing its identifiers. A changed list performs one
immediate `PluginStore` write, so session resume observes the same current
checklist. Unloading removes the Tool while retaining session data.

Todo items track concrete work. They do not own the durable session objective;
that belongs to the separate Goal plugin.
