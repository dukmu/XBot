# `goal`

Session-scoped completion condition with an automatic evaluator loop. The
human sets a condition; an independent evaluator model judges it after every
turn; the loop keeps starting turns until the condition is met or judged
impossible. This mirrors Claude Code's `/goal`, which is a session-scoped
prompt-based Stop hook rather than an Agent-managed record.

- **Import/profile:** `goal`, Agent profile.
- **Source:** `XBotv2/goal/plugin.py`, `XBotv2/goal/models.py`,
  `XBotv2/goal/evaluator.py`.
- **Injects/provides:** `commands`, `engine`, `model`, `state` → `goal`
  (`GoalService`); optionally reads `jobs`, `todolist`, `usage`.
- **Subscribes to events:** `turn/start`, `after/tool-call`, `turn/end`,
  `error`, `session/resume`, `session/close`.
- **Command:** `/goal`, `/goal <condition>`, `/goal clear`.
- **Agent tools:** `create_goal` (start/replace a goal) and `get_goal` (read
  it). No Agent tool can end a goal: completion and impossibility are the
  evaluator's call.
- **Events:** `COLLECT_STATUS_SLOTS` (`goal`, `goal_round`, `goal_reason`,
  `goal_stats`), `session/history-changed` (post-compaction restatement), and a
  `goal_updated` `ClientEvent` on every transition.

## Public data models

### `GoalService` (`XBotv2/goal/plugin.py`)

```python
class GoalService:
    def __init__(
        self, store: StateService, engine: AgentLoopDriverPort, *,
        model: ModelPort, events: Context | None = None,
        jobs: JobsPort | None = None, usage: UsagePort | None = None,
        todolist_getter: Callable[[], Any | None] | None = None,
        config: GoalConfig | None = None, now: Callable[[], float] = time.time,
    ) -> None: ...

    async def snapshot(self) -> GoalSnapshot | None: ...
    async def command(self, raw_args: str) -> CommandResult: ...
    async def status(self) -> ToolResult: ...          # backs get_goal
    async def set_condition(self, condition: str) -> ToolResult: ...  # backs create_goal
    async def clear(self) -> ToolResult: ...
    async def contribute_status(self, slots: StatusSlots) -> None: ...
    async def on_compaction(self, event: HistoryChanged) -> None: ...
    async def _start_round(self, goal: GoalSnapshot, *, reason: str = "", note: str = "") -> None: ...
```

### `GoalConfig`

```python
class GoalConfig(BaseModel):
    checkin_seconds: float = 1_800.0     # first check-in delay
    checkin_max_factor: float = 4.0      # backoff cap
    max_retries: int = 3
    retry_seconds: float = 30.0          # doubles per retry
    stall_turns: int = 3
    max_idle_checkins: int = 3
    max_rounds: int = 20                 # hard round cap
```

### Agent tools

| Tool | Behavior |
|---|---|
| `create_goal(condition)` | Create or replace the session goal and start a turn with the condition as the directive |
| `get_goal()` | Read condition, status, turns evaluated, tokens, latest verdict/reason |

Both are `kind="think"` and in the default permission allow-list. There is
deliberately no `complete_goal`/`block_goal`: self-certification is what the
evaluator exists to replace. An Agent that believes the condition is
impossible says so in its reply, and the evaluator reads that from the
transcript.

### Round prompts and model context

There is **no per-request projection**: the goal is carried by persisted turns.

1. **Round 1** — `set_condition` / `create_goal` queues one persisted,
   self-contained round prompt and admits it as the turn directive:

```
<system_reminder source="goal" event="round" round="1" max="20">
Objective: "ship the API"
Round: 1/20

Continue working toward the objective in this same session. ...
</system_reminder>
```

2. **Later rounds** — each `not_yet_met` verdict queues round `n+1` with the
   same objective, the round number, and the evaluator's reason. Every round is
   therefore self-contained and survives compaction on its own.
3. **Compaction** — `on_compaction` listens for `HISTORY_CHANGED` with a
   `compact:` operation (the session-owned contract, so no dependency on the
   optional `compact` plugin) and injects one state block:

```
<system_reminder source="goal" event="compaction" round="2" max="20">
Active goal: "ship the API"
Round: 2/20
Latest evaluator verdict: test/auth still fails
</system_reminder>
```

Round prompts are queued with `engine.followup` (they must wake a turn); the
compaction block uses `engine.inject` (it must not).

### Attribution

Every queued or injected turn carries `source="goal"` plus
`metadata={"kind": "round" | "compaction", ...}`, so
`runtime_input_value` records
`additional_kwargs["runtime_input"] = {"source", "event"}`. The XML attributes
use the same two labels, and clients render the turn as a runtime entry instead
of typed human input. The round number is not duplicated into that provenance:
the goal snapshot owns `turns_evaluated`, and the XML carries it for the model.

## Status surface

`contribute_status` adds:

- `goal: <status>`
- `goal_round: "<current>/<max>"` while active
- `goal_reason: <latest reason>` when a reason exists
- `goal_stats: "<tokens>tok <n>tools <done>/<total>tasks"` for a terminated goal
  with non-zero consumption

`/goal` prints the full record, including duration.

## Migration

A v1/v2 snapshot (`objective`, `summary`, `stats.turns`, `token_budget`) is
read as v3 on load: `objective → condition`, `complete → achieved`,
`blocked → failed`, `summary → reason`, and unknown stat keys are dropped.

## How `apply()` works

```python
def apply(self, ctx: Context, config: GoalConfig | None = None) -> None:
    service = GoalService(
        ctx.state.namespace(self.name), ctx.engine, model=ctx.model, events=ctx,
        jobs=ctx.get("jobs", strict=False), usage=ctx.get("usage", strict=False),
        todolist_getter=lambda: ctx.get("todolist", strict=False), config=config,
    )
    ctx.set("goal", service)
    ctx.dispose(service.dispose)
    ctx.on(Events.TURN_START, service.on_turn_start)
    ctx.on(Events.AFTER_TOOL_CALL, service.on_tool_call)
    ctx.on(Events.TURN_END, service.on_turn_end)
    ctx.on(Events.ON_ERROR, service.on_error)
    ctx.on(Events.SESSION_RESUME, service.on_session_resume)
    ctx.on(Events.SESSION_CLOSE, service.on_session_close)
    ctx.on(COLLECT_STATUS_SLOTS, service.contribute_status)
    ctx.commands.register(Command(
        name="goal", handler=service.command,
        usage="/goal | /goal <condition> | /goal clear", exclusive=False,
    ))
```

Agent tools are registered alongside the command:

```python
for function in (service.create_goal, service.get_goal):
    ctx.tools.register(replace(Tool.from_function(function), kind="think"))
```

No Agent tool can end a goal: only the evaluator (or a human `/goal clear`)
does.

## On-disk artifacts

```json
{"goal.snapshot": {"condition": "all tests pass", "status": "active",
  "reason": "test/auth still fails", "turns_evaluated": 1,
  "started_at": 1710000000.0, "finished_at": 0.0, "retries": 0,
  "tool_less_turns": 0, "idle_checkins": 0, "checkin_seconds": 0.0,
  "stalled": false, "stats": {"tool_calls": 0, "input_tokens": 0,
  "output_tokens": 0, "total_tokens": 0, "todo_items": 0,
  "todo_completed": 0}, "schema_version": 3},
 "goal.usage_baseline": {"input_tokens": 0, "output_tokens": 0,
  "total_tokens": 0}}
```

## Cross-references

- Depends on: `commands`, `engine`, `model`, `state`, `tools`, `agentloop`
  (`TURN_START`, `AFTER_TOOL_CALL`, `TURN_END`, `ON_ERROR`, `SESSION_RESUME`,
  `SESSION_CLOSE`), `session` (`HISTORY_CHANGED`),
  `application` (`COLLECT_STATUS_SLOTS`, `RUNTIME_EVENT`),
  `llm` (`invoke_llm`).
- Optionally reads: `jobs`, `todolist`, `usage`.
- Pairs with: `todolist` (Goal reads task progress; tasks never depend on Goal).

## Common pitfalls

- **Expecting an Agent tool to complete a goal**: there is none. Only the
  evaluator ends a goal, from observed conversation evidence.
- **Expecting a per-turn goal projection**: there is none. The goal reaches
  the model through its round prompts and the post-compaction state block, so
  `get_goal` is the way to read a terminated or replaced record.
- **Assuming `goal_stats` exists**: it is only added for a terminated goal with
  non-zero consumption.
- **Expecting evaluation inside `turn/end`**: it runs in a scheduled task after
  the turn finished, so `turn_finished` still carries `goal: active` for the
  turn that triggered it.
- **Leaving timers running**: `ctx.dispose` cancels them; a service created
  without `dispose` can wake up after the session closed.
