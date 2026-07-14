# TodoList Plugin

`builtin_plugins/todolist` provides explicit, session-scoped work tracking. It
uses only the public tool and `PluginStore` APIs and does not infer tasks from
assistant prose.

## Tools

| Tool | Behavior |
|---|---|
| `list_todos` | Return items in creation order. |
| `create_todo` | Create a `pending` item with a stable ID. |
| `update_todo` | Change content, status, or both. |
| `remove_todo` | Remove one item by ID. |

The supported statuses are `pending`, `in_progress`, and `completed`. Tool
results include structured item data as well as concise model-readable text.
Invalid input and unknown IDs return structured tool errors without changing
the stored list. These host tools remain subject to the core permission policy;
the plugin does not bypass or duplicate authorization.

## Persistence And Scope

The plugin stores `next_id` and the ordered item list together in one
`PluginStore` value. Each successful mutation performs one immediate persisted
write, so session resume observes the same list and new IDs are not reused.
Unloading the plugin removes its registered tools through the normal plugin
lifecycle but deliberately retains session data for a later reload.

Todo items track concrete work. They do not own the durable session objective;
that belongs to the separate Goal plugin.
