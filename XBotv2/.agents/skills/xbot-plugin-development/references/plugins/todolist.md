# `todolist`

Thread-scoped task list and the four fine-grained task tools. Owns one task
list per thread that persists across turns and validates every mutation.

- **Import/profile:** `todolist`, Agent profile.
- **Source:** `XBotv2/todolist/plugin.py`,
  `XBotv2/todolist/contracts.py`.
- **Injects/provides:** `tools`, `state` → `todolist` (`TaskService`).
- **Subscribes to events:** `turn/start` (stale-list accounting),
  `after/context-components-build` (task reminder).
- **Operations:** `GET_TODOS` (`EmptyRequest → TaskList`).
- **Tools:** `task_create`, `task_get`, `task_update`, `task_list`.
- **Commands:** none. Tasks are managed by the Agent and read by clients.
- **Injects:** `engine.inject` for the stale-task reminder and the
  post-compaction restatement only; no per-request projection.
- **Client event:** `todo_updated` (emitted on every mutating call).

The tool set mirrors Claude Code's task system (`TaskCreate` / `TaskGet` /
`TaskUpdate` / `TaskList`), which is the default there; `TodoWrite` is only its
`CLAUDE_CODE_ENABLE_TASKS=0` fallback. XBotv2 has no such switch, so the
fine-grained tools are the only surface. Names use this repository's snake_case
tool convention.

## Public data models

### `TaskService` (`XBotv2/todolist/plugin.py`)

```python
class TaskService:
    """Own the typed task list for one thread."""

    def __init__(self, store: StateService, *, agent_name: str = "") -> None: ...

    async def snapshot(self) -> TaskList: ...

    async def task_create(
        self, subject: str, description: str = "", activeForm: str = ""
    ) -> ToolResult: ...

    async def task_get(self, taskId: str) -> ToolResult: ...

    async def task_update(
        self,
        taskId: str,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        activeForm: str | None = None,
        owner: str | None = None,
        addBlocks: list[str] | None = None,
        addBlockedBy: list[str] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ToolResult: ...

    async def task_list(self) -> ToolResult: ...

    async def get_snapshot(self, _request: EmptyRequest) -> TaskList: ...

    async def on_turn_start(self, _event: EventContext) -> None: ...

    async def on_compaction(self, event: HistoryChanged) -> None: ...
```

### `Task` and `TaskList` (`XBotv2/todolist/contracts.py`)

```python
class Task(BaseModel):
    id: str
    subject: str
    description: str = ""
    activeForm: str = ""
    owner: str = ""
    status: Literal["pending", "in_progress", "completed"] = "pending"
    blocks: tuple[str, ...] = ()
    blockedBy: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = {}

class TaskList(BaseModel):
    schema_version: Literal[2] = 2
    next_id: int = 1          # id high-watermark; deleted ids are never reused
    tasks: tuple[Task, ...] = ()

    def find(self, task_id: str) -> Task | None: ...
    def allocate_id(self) -> str: ...
    def replace(self, task: Task) -> "TaskList": ...
    def remove(self, task_id: str) -> "TaskList": ...
    def projection(self) -> dict[str, JsonValue]: ...
```

`deleted` is an update instruction, never a stored status, so
`status` accepts `pending | in_progress | completed | deleted` only on input.

### `TaskConfig`

```python
class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int = 50
    max_subject_chars: int = 500
    max_description_chars: int = 2_000
    max_active_form_chars: int = 200
    reminder_after_turns: int = 3
    verification_nudge: bool = True
    verification_hint: str = "verif"       # case-insensitive regex
```

These guardrails are owned by this plugin (they keep one thread's list out of
unbounded model context); Claude Code's tool contract does not define them.
They are mounted as the plugin's `Config`, so any tree layer can override them:

```yaml
- id: todolist
  name: todolist
  config:
    max_tasks: 50
    reminder_after_turns: 3
    verification_nudge: true
```

The configured text bounds are also written into each tool's JSON schema
(`maxLength`, including the `anyOf` branch on `task_update`), so the model sees
the limits it is validated against instead of only failing on them.

### `TaskValidationError`

Codes: `invalid_task`, `invalid_task_id`, `invalid_task_ids`,
`invalid_task_status`, `invalid_task_update`, `invalid_task_dependency`,
`blocked`, `task_not_found`, `task_limit`.

## Task lifecycle

1. **Created** — `task_create` appends a `pending` task and returns its id.
2. **Activated** — `task_update(status="in_progress")`; a task whose
   `blockedBy` entries are not all `completed` is refused with `blocked`.
   When the task has no owner, the claiming agent becomes the owner.
3. **Completed** — `task_update(status="completed")`.
4. **Deleted** — `task_update(status="deleted")` removes the task and every
   dependency edge that referenced it.

`addBlocks` / `addBlockedBy` maintain both sides of an edge
(`A.blocks += B` and `B.blockedBy += A`), and ids come from a monotonic
watermark so a deleted id is never reused.

## Verification nudge

When an update completes the last outstanding task, the list holds at least
three tasks, and no subject matches `/verif/i`, the tool result appends a
notice telling the model to verify the work independently (in Claude Code,
with a verification subagent) before reporting it done.

## Model context: no per-request projection

The task list does not need a per-turn injection: `task_create`/`task_update`
arguments and results are the list, and they stay in the conversation. Nothing
is added to the system prompt or rebuilt per request. Two persisted reminders
are the only context the plugin authors:

### Stale-task reminder

`on_turn_start` counts turns since the last task mutation. Once the count
reaches `reminder_after_turns` (default 3) while outstanding work remains, the
plugin injects one persisted user-role reminder through `engine.inject`:

```
<system_reminder source="todo" event="reminder">
The task tools haven't been used recently. ...
</system_reminder>
```

- Text only: the reminder never restates the list, because the tool calls
  already carry it.
- The counter resets after each nudge, so it recurs every
  `reminder_after_turns` turns rather than on every request.
- `inject` does not wake a turn; a reminder never starts new work.
- Skipped when the list is empty or everything is complete.

### Post-compaction reminder

`on_compaction` listens for `HISTORY_CHANGED` with a `compact:` operation (the
session-owned contract, so the plugin does not depend on the optional `compact`
plugin). After history is summarized the tool calls that carried the list are
gone, so this is the one place outstanding work is restated:

```
<system_reminder source="todo" event="compaction">
Unfinished tasks (owned by task_update):
- #2 [ ] open step · blocked by #1
</system_reminder>
```

Nothing is injected when no task is outstanding, and other history mutations
(undo, clear) do not trigger it.

### Attribution

Both reminders are injected with `source="todo"` and
`metadata={"kind": "reminder" | "compaction"}`, so `runtime_input_value`
records `additional_kwargs["runtime_input"] = {"source", "event"}`. The live
`message` event and `SessionHistoryItem.runtime` both carry it, and the XML
attributes use the same two labels. Clients therefore render a reminder as a
runtime entry instead of inventing typed human input, and never parse the XML
to learn where a message came from.

## How `apply()` works

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    service = TaskService(ctx.state.namespace(self.name), agent_name=...)
    ctx.set("todolist", service)
    ctx.on(GET_TODOS.name, service.get_snapshot)
    ctx.on(Events.TURN_START, service.on_turn_start)
    ctx.on(HISTORY_CHANGED, service.on_compaction)
    for function in (
        service.task_create, service.task_get,
        service.task_update, service.task_list,
    ):
        ctx.tools.register(replace(Tool.from_function(function), kind="think"))
```

The four tools are in the default permission allow-list, so task management
never prompts.

## On-disk artifacts

`TaskService` uses `ctx.state.namespace("todolist")` — persisted as:

```json
{"todolist.snapshot": {"schema_version": 2, "next_id": 3, "tasks": [
  {"id": "1", "subject": "Do the thing", "description": "",
   "activeForm": "Doing the thing", "owner": "", "status": "in_progress",
   "blocks": [], "blockedBy": [], "metadata": {}}]},
 "todolist.stale_turns": 0}
```

Upstream stores one file per task under `~/.claude/tasks/<list>/<id>.json`
plus `.highwatermark`, because its task list is shared across concurrent
agents. XBotv2's `StateService` is one atomic JSON document per thread with a
single writer, so the whole list is stored as one document with the same
watermark semantics.

A schema v1 snapshot (`{"schema_version": 1, "items": [...]}`) is migrated on
read: each item becomes a task with an id assigned in list order and

## Cross-references

- Depends on: `tools`, `state`, `agentloop` (`TURN_START`, `GET_TODOS`),
  `session` (`HISTORY_CHANGED`).
- Depended on by: the Agent (task tools) and the optional `goal` plugin, which
  reads the list to report `todo_items`/`todo_completed`. Tasks do not depend
  on Goal.
- Pairs with: `goal` (Goal observes the list; the list stays independent).

## HTTP projection

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/todos",
    operation_id="get_todos",
)
async def get_todos(session_id: str, thread_id: str) -> TaskList: ...
```

The route returns the typed `TaskList` (`{schema_version, next_id, tasks}`);
the `todo_updated` client event carries the same fields plus
`"kind": "todo_snapshot"`. The client event name and route keep their original
"todo" spelling, because `XBotv2/jobs` already owns the client-side "task"
vocabulary for background jobs. The update path remains an Agent tool; clients
must not mutate the list by writing the persistence file.

## Common pitfalls

- **Replacing the whole list**: there is no bulk replace any more. Use
  `task_create` for new work and `task_update` for status; deleting is
  `task_update(status="deleted")`.
- **Starting blocked work**: `task_update(status="in_progress")` fails with
  `blocked` while any `blockedBy` task is not `completed`.
- **Assuming completion clears the list**: unlike the v1 `TodoWrite`
  behaviour, completed tasks stay until deleted, so the list is a durable
  record.
- **Reusing ids**: never assume `#1` is free after deletion; the watermark
  moves forward.
