# Goal Plugin

`builtin_plugins/goal` maintains one durable session objective. It separates
the reason for the work from the concrete steps tracked by TodoList.

## Tools

| Tool | Behavior |
|---|---|
| `create_goal` | Create an active goal when none is active. |
| `inspect_goal` | Return the active or most recent terminal goal. |
| `update_goal` | Replace the active objective. |
| `complete_goal` | Mark the active goal completed. |
| `abandon_goal` | Mark the active goal abandoned. |

The statuses are `active`, `completed`, and `abandoned`. An objective is
required and limited to 2,000 characters. Creating another goal while one is
active fails explicitly; complete or abandon the active goal first. A new goal
may replace the most recent terminal goal.

## Context And Persistence

Every successful mutation writes the single `goal` value immediately through
`PluginStore`. Terminal goals remain inspectable after resume but are not
active. While a goal is active, the plugin appends one concise public
`ContextComponent` during `AFTER_CONTEXT_COMPONENTS_BUILD`:

```text
## Active Goal

<objective>
```

This component is rebuilt for model context and is never written to message
history. Unload removes the Hook and tools but retains the session goal. Goal
tools remain subject to the core permission policy.

The plugin does not infer goals from conversation text, create todo items, or
automatically continue turns. Automatic continuation requires a separate
behavior contract before implementation.
