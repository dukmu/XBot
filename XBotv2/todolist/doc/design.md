# Thread task list

The todolist plugin owns task rows, dependency edges, counters and reminder state. Its registered `task_create`, `task_get`, `task_update`, `task_delete` and `task_list` tools use the normal tool execution path. Reminders and compaction may add runtime notices to prompts. Defaults are `max_tasks: 50`, `max_subject_chars: 500`, `max_description_chars: 2000`, `max_active_form_chars: 200`, `reminder_after_turns: 3` and `verification_nudge: true`.

`max_tasks` limits pending and in-progress tasks; completed tasks do not occupy an active slot. Reopening a completed task requires a free slot and must satisfy dependency constraints. Completed tasks and their full details remain in SQL and can be read by `task_get`. Use `task_list(completed=true, after=0, limit=100)` to read completed history in numeric ID order; the result states the last ID to use as the next `after` cursor. Limits are 1–400, and pages can be traversed without a total-history cap.

Dispatch `GET_TODOS` with `EmptyRequest()` to read the active `TaskList`; `TaskChanged` and the existing HTTP todos endpoint use the same active view. `tasks` contains only pending and in-progress tasks, `completed_count` counts completed tasks, and `edges` describes dependencies of active tasks. `completed_prerequisites` identifies completed prerequisites referenced by those edges without retaining their full task contents. The model does not include historical completed tasks in the snapshot.

Dispatch `GET_COMPLETED_TODOS` with `CompletedTasksRequest(after=0, limit=100)` to read completed tasks, or GET `/sessions/{session_id}/threads/{thread_id}/todos/completed?after=0&limit=100`. The page's `next_after` is the cursor for the next page, or null at the end. Task IDs are stable numeric strings. A full last page may yield a cursor followed by an empty page.

Tasks and edges are copied with the session's consistent database snapshot during fork. Todo validates cycles, missing dependencies and active transitions in its own short transactions; a rejected update does not change the stored graph. Persistence owns the database worker and physical I/O.


The owner maintains active and completed counts in the same transaction as each task change. Active snapshots and completed pages use the status and dependency indexes. An unsupported counter schema fails explicitly and is not migrated automatically.
