# `goal`

Persistent thread Goal state and automatic continuation. Owns one
Goal per thread that survives across turns and is persisted to the
thread's `StateService`.

- **Import/profile:** `goal`, Agent profile.
- **Source:** `XBotv2/goal/plugin.py`,
  `XBotv2/goal/models.py`.
- **Injects/provides:** `tools`, `commands`, `engine`, `state` → `goal`
  (`GoalService`).
- **Subscribes to events:** `turn/start` (continuation scheduling),
  `turn/end` (auto-restart if not interrupted).
- **Tool operations:** `create_goal`, `get_goal`, `update_goal`.
- **Command:** `/goal [set|pause|resume|clear|complete|block]`.
- **Events:** `COLLECT_STATUS_SLOTS` (contributes status to slots).

## Public data models

### `GoalService` (`XBotv2/goal/plugin.py:25-190`)

```python
class GoalService:
    """Own one thread's typed Goal state and continuation scheduling."""

    def __init__(self, store: StateService, engine: AgentLoopDriverPort) -> None:
        self._store = store
        self._engine = engine
        self._continuation_pending = False

    async def snapshot(self) -> GoalSnapshot | None: ...

    async def create_goal(
        self,
        objective: str,
        token_budget: int | None = None,
    ) -> ToolResult: ...

    async def get_goal(self) -> ToolResult: ...

    async def update_goal(
        self,
        status: Literal["complete", "blocked"],
        summary: str,
    ) -> ToolResult: ...

    async def command(self, raw_args: str) -> CommandResult: ...

    async def contribute_status(self, slots: StatusSlots) -> None: ...

    async def start_goal_turn(self, event: EventContext) -> None: ...

    async def on_turn_end(self, event: EventContext) -> None: ...

    async def start(self) -> None: ...
```

### `GoalSnapshot` (`XBotv2/goal/models.py`)

```python
class GoalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    objective: str
    summary: str = ""
    token_budget: int | None = None
    status: Literal["active", "paused", "complete", "blocked"] = "active"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
```

`GoalStatus = Literal["active", "paused", "complete", "blocked"]`.
`GOAL_STATUSES` is the tuple of allowed values.

### `StatusSlots`

```python
class StatusSlots:
    def add(self, key: str, value: str) -> None: ...
```

Used by `COLLECT_STATUS_SLOTS` to contribute `"goal": goal.status`.

## Tool operations

### `create_goal`

```python
async def create_goal(self, objective: str, token_budget: int | None = None) -> ToolResult:
    """Create the persistent session goal explicitly requested by the human.

    Call only when the human explicitly asks the Agent to create a
    persistent Goal. Complexity, duration, or a Todo list is not such a
    request. Never rewrite the human's objective into a new Goal.
    """
```

Validates: objective is non-empty (max 2000 chars), token_budget is
positive integer or None, no active goal exists.

### `get_goal`

```python
async def get_goal(self) -> ToolResult:
    """Read the current session Goal without changing or advancing it."""
```

Returns formatted goal or `"No goal has been created."`.

### `update_goal`

```python
async def update_goal(
    self,
    status: Literal["complete", "blocked"],
    summary: str,
) -> ToolResult:
    """Finish the active Goal after reaching a terminal outcome."""
```

Validates: status is "complete" or "blocked", summary is non-empty
(max 2000 chars), an active goal exists.

## Command — `/goal`

```
/goal | /goal [--token-budget <tokens>] <objective> |
        /goal pause|resume|clear|complete <summary>|block <summary>
```

| Action | Args | Behavior |
|---|---|---|
| (none) | (none) | `get_goal()` |
| `set` | `[--token-budget N] <objective>` | Creates/resets goal |
| `pause` | (none) | Sets status to "paused" |
| `resume` | (none) | Sets status to "active", starts continuation |
| `clear` | (none) | Deletes snapshot, returns "No goal has been created." |
| `complete` | `<summary>` | Sets status="complete" |
| `block` | `<summary>` | Sets status="blocked" |

All actions (except `get`) that succeed trigger `await self.start()`
for continuation.

## Continuation scheduling

```python
async def start(self) -> None:
    """Schedule the next active-goal turn if one is not already pending."""
    goal = await self._active_goal()
    if goal is None or self._continuation_pending:
        return
    self._continuation_pending = True
    await self._engine.followup(
        "[goal continuation]",
        source="goal",
        metadata={"continuation": True},
    )
```

`start_goal_turn()` on `turn/start`: if `event.continuation` is True,
injects the goal context into `event.user_input`:

```python
event.user_input = _goal_context(goal)
# Returns: {"objective": "...", "status": "...", ...}
```

`on_turn_end()` on `turn/end`: if `event.stop_reason == "client_interrupt"`,
pauses the goal. Otherwise calls `start()` for automatic continuation.

## How `apply()` works

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    service = GoalService(ctx.state.namespace(self.name), ctx.engine)
    ctx.set("goal", service)
    ctx.on(Events.TURN_START, service.start_goal_turn)
    ctx.on(Events.TURN_END, service.on_turn_end)
    ctx.on(COLLECT_STATUS_SLOTS, service.contribute_status)
    ctx.tools.register(Tool.from_function(service.create_goal, name="create_goal"))
    ctx.tools.register(Tool.from_function(service.get_goal, name="get_goal"))
    ctx.tools.register(Tool.from_function(service.update_goal, name="update_goal"))
    ctx.commands.register(Command(
        name="goal",
        description="Set or manage the persistent session goal.",
        handler=service.command,
        usage="/goal | /goal [--token-budget <tokens>] <objective> | ...",
        examples=("/goal Stabilize the C/S API", "/goal pause", ...),
        exclusive=False,
    ))
```

## On-disk artifacts

`GoalService` uses `ctx.state.namespace("goal")` — persisted as:

```json
{"objective": "Do the thing", "status": "active", "token_budget": 1000,
 "created_at": 1234567890.0, "updated_at": 1234567890.0}
```

## Cross-references

- Depends on: `tools`, `commands`, `engine`, `state`,
  `agentloop` (`TURN_START`, `TURN_END`), `application` (`COLLECT_STATUS_SLOTS`).
- Depended on by: the Agent (goal management tools).
- Pairs with: `agentloop` (continuation scheduling via `engine.followup`).

## Common pitfalls

- **Calling `create_goal` when a goal already exists**: raises
  `"goal_exists"` error. Must complete, block, or clear first.
- **Calling `update_goal` with `status="paused"`**: only `"complete"`
  and `"blocked"` are valid terminal statuses. `"paused"` is only
  set via `/goal pause`.
- **Expecting `get_goal` to auto-start**: it reads without advancing.
  Use `resume` or `start()` for continuation.
- **Token budget validation**: `type(value) is int and value > 0` —
  booleans are rejected (since `bool` is a subclass of `int` in Python).
- **`exclusive=False` on `/goal`**: the command does not close the
  user's pending input — useful for goal management during a turn.
- **Continuation is only scheduled on non-interrupt turn ends**:
  if `event.stop_reason == "client_interrupt"`, the goal is paused
  instead of continued.
