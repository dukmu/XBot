# `todolist`

Owns one persisted typed task list per Agent thread. It exposes five Agent
Tools and a read-only server projection; there is no human slash command.

- **Source:** `XBotv2/todolist/plugin.py`, `contracts.py`, `protocol.py`.
- **Profiles:** Agent and server (the Agent facet uses Tools/state; the server
  facet exposes the session-backed HTTP read route).
- **Tools:** `task_create`, `task_get`, `task_update`, `task_delete`,
  `task_list`.
- **Operation/route:** `GET_TODOS` (`EmptyRequest -> TaskList`), exposed as
  `GET /sessions/{session_id}/threads/{thread_id}/todos`.
- **Events:** observes `TURN_START` and session `HISTORY_CHANGED`; emits an
  application `RuntimeEvent` containing `TaskChanged(snapshot=...)` after a
  successful mutation.

Task state is stored as one validated `TaskList` snapshot in the `todolist`
StateService namespace. The list contains `version`, monotonic `next_id`,
`tasks`, and dependency `edges`. A `Task` has `id`, `subject`, `description`,
`active_form` (serialized with alias `activeForm`), `owner`, and status
`pending | in_progress | completed`. `DependencyEdge` stores a prerequisite
and dependent task ID. The list rejects missing/self/cyclic edges; a task
cannot become `in_progress` while prerequisites are unfinished. Deleting a
task removes connected edges. Deletion is a separate `task_delete` Tool, not a
status value.

## Tool call parameters

The callable names below are the current Tool-schema parameter names:

```python
task_create(subject: str, description: str = "", active_form: str = "")
task_get(taskId: str)
task_update(
    taskId: str,
    status: TaskStatusInput | None = None,
    subject: str | None = None,
    description: str | None = None,
    active_form: str | None = None,
    owner: str | None = None,
    add_blocks: list[str] | None = None,
    add_blocked_by: list[str] | None = None,
)
task_delete(task_id: str)
task_list()
```

`task_update` uses additive dependency arguments and reports a blocked
transition as a typed `ToolFailed`; malformed input and unknown task IDs also
return failures. Successful create/update/delete calls persist the snapshot
and publish `TaskChanged`. `task_list`/`task_get` are read-only.

## Configuration and lifecycle

`TaskConfig` supplies validated bounds and behavior: `max_tasks` (50),
`max_subject_chars` (500), `max_description_chars` (2,000),
`max_active_form_chars` (200), `reminder_after_turns` (3),
`verification_nudge` (true), and `verification_hint` (`"verif"`). The
configured text limits are reflected in Tool argument schemas.

When unfinished tasks have gone unused for the configured number of turns,
the plugin inserts a persisted `NEXT_STEP` runtime reminder. After a
compaction history change it restates unfinished tasks once, because previous
task Tool calls may no longer be in the effective surface. These are runtime
inputs, not a second task-state store.

The implementation uses XBot's own task model and does not promise Claude
Code task API compatibility. Read the source contracts before relying on
serialized JSON field aliases.
