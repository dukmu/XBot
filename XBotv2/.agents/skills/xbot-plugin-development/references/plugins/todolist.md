# `todolist`

Thread-scoped Todo state and tool projection. Owns one Todo list per
thread that persists across turns and is validated on each update.

- **Import/profile:** `todolist`, Agent profile.
- **Source:** `XBotv2/todolist/plugin.py`,
  `XBotv2/todolist/models.py`,
  `XBotv2/todolist/contracts.py`.
- **Injects/provides:** `tools`, `state` → `todolist`
  (`TodolistService`).
- **Operations:** `GET_TODOS` (`EmptyRequest → TodoSnapshot`).
- **Tool:** `update_todos` (replaces the full checklist).
- **Client event:** `todo_updated` (emitted on every successful update).

## Public data models

### `TodolistService` (`XBotv2/todolist/plugin.py:27-100`)

```python
class TodolistService:
    """Own the typed Todo snapshot for one thread."""

    def __init__(self, store: StateService) -> None:
        self._store = store

    async def snapshot(self) -> TodoSnapshot:
        """Return the current snapshot. Returns empty if none stored."""

    async def update_todos(
        self, todos: list[dict[str, str]]
    ) -> ToolResult:
        """Replace the current Todo checklist with one complete list."""

    async def get_snapshot(self, _request: EmptyRequest) -> TodoSnapshot: ...
```

### `TodoSnapshot` (`XBotv2/todolist/models.py`)

```python
class TodoSnapshot(BaseModel):
    items: tuple[TodoItem, ...] = ()

    @classmethod
    def from_items(cls, items: list[dict[str, str]]) -> "TodoSnapshot": ...
    def projection(self) -> list[dict[str, Any]]: ...

@dataclass(frozen=True, slots=True)
class TodoItem:
    content: str
    status: Literal["pending", "in_progress", "completed"]
```

### `TodoValidationError`

```python
class TodoValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
```

Codes: `"invalid_todo_progress"` (more than one `in_progress`),
`"invalid_item"` (missing required fields), etc.

### `update_todos` schema

```python
_UPDATE_TODOS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["todos"],
}
```

## Update validation rules

1. **All items must have `content` (minLength: 1) and `status`:**
   `TodoSnapshot.from_items()` validates each item.
2. **Exactly one `in_progress` when work remains:** if any item is
   unfinished and `in_progress` count != 1, returns
   `ToolResult.failure("invalid_todo_progress", ...)`.
3. **All completed → cleared:** if all items are `completed`, the
   stored snapshot is cleared to empty.
4. **Empty list → discarded:** if the submitted list is empty, the
   checklist is cleared (for obsolete checklists).

## Response semantics

| Scenario | Content | Data (projection) | Client event |
|---|---|---|---|
| All completed | "All todos completed; the active checklist was cleared." | `{items: [], in_progress: 0, ...}` | `todo_updated` |
| All completed (already empty) | "Todo list is already empty." | same as before | `todo_updated` |
| Cleared | "Todo list cleared." | `{items: [], ...}` | `todo_updated` |
| Updated | "Todo list updated." | `{items: [...], in_progress: 1, ...}` | `todo_updated` |
| Unchanged | "Todo list unchanged.\nDo not call update_todos again until the work changes." | same | `todo_updated` |

## How `apply()` works

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    service = TodolistService(ctx.state.namespace(self.name))
    ctx.set("todolist", service)
    ctx.on(GET_TODOS.name, service.get_snapshot)
    ctx.tools.register(
        Tool(
            name="update_todos",
            description=inspect.getdoc(service.update_todos) or "",
            function=service.update_todos,
            parameters=_UPDATE_TODOS_SCHEMA,
        ),
    )
```

## On-disk artifacts

`TodolistService` uses `ctx.state.namespace("todolist")` — persisted as:

```json
{"items": [{"content": "Do the thing", "status": "in_progress"}]}
```

## Cross-references

- Depends on: `tools`, `state`, `agentloop` (`GET_TODOS` operation).
- Depended on by: the Agent (todo management tool).
- Pairs with: `goal` (both use `StateService` namespace pattern).

## Common pitfalls

- **Submitting multiple `in_progress` items**: validation rejects
  with `"invalid_todo_progress"`. Exactly one must be in progress
  when work remains.
- **Not including unfinished items in `update_todos`**: the tool
  **replaces** the entire list, not appends. Unfinished items must
  be included with their current status.
- **Submitting an empty list to "complete" all todos**: use status
  `"completed"` for each item instead. Empty list is only for
  discarding obsolete checklists.
- **Adding a summary/final reply item**: the documentation explicitly
  says "Do not add an item for the final reply or summary."
- **Assuming `update_todos` is idempotent**: if the submitted list
  matches the current one exactly, the response says "unchanged"
  and warns "Do not call update_todos again until the work changes."
