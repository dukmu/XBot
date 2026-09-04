# `compact`

Semantic conversation history compaction — summarize old turns to
free context tokens while preserving recent conversation state.

- **Import/profile:** `compact`, Agent profile.
- **Source:** `XBotv2/compact/plugin.py`,
  `XBotv2/compact/service.py`,
  `XBotv2/compact/config.py`,
  `XBotv2/compact/commands.py`,
  `XBotv2/compact/tools.py`,
  `XBotv2/compact/compactor.py`,
  `XBotv2/compact/history.py`,
  `XBotv2/compact/summary.py`,
  `XBotv2/compact/events.py`,
  `XBotv2/compact/protocol.py`.
- **Injects/provides:** `tools`, `commands`, `model`, `loop_state`,
  `usage` → `compact` (`CompactService`).
- **Subscribes to events:** `before/context` (manual trigger via
  `_on_before_context`), `before/model-request` (automatic trigger).
- **Emits:** `pre/compact` (`BeforeCompact`), `post/compact`
  (`AfterCompact`), `session/history-changed` (`HistoryChanged`).
- **Tool:** `compact` (requests manual compaction).
- **Command:** `/compact` (immediate compaction).

## Public data models

### `CompactService` (`XBotv2/compact/service.py:37-210`)

```python
class CompactService:
    """Own compaction runtime state, proposal generation, and commit semantics."""

    def __init__(
        self,
        *,
        events: CompactEventsPort,
        model: Any,
        state: Any,
        usage: UsagePort,
        config: CompactConfig,
    ) -> None:
        self._events = events
        self.model = model
        self.state = state
        self._usage = usage
        self._automatic = config.automatic
        self._output_reservation = config.output_reservation
        self._trigger_ratio = config.trigger_ratio
        self._keep_recent_turns = config.keep_recent_turns
        self._summary_max_chars = config.summary_max_chars
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""
        self._last_compaction: dict[str, Any] = {}

    async def _dispose(self) -> None:
        """Clear all mutable state. Called by ctx.dispose."""

    def request_manual_compaction(self) -> None:
        """Flag manual compaction; consumed on next BEFORE_CONTEXT."""

    def _consume_manual_request(self, session: Any = None) -> bool:
        """Return True if a manual request was consumed."""

    async def _compact_command(self, raw_args: str) -> CommandResult:
        """Handle /compact slash command."""

    async def _on_before_context(self, ctx: EventContext) -> dict[str, Any] | None:
        """Manual compaction trigger via short-circuit."""

    async def _on_before_model_request(self, ctx: EventContext) -> dict[str, Any] | None:
        """Automatic compaction trigger — checks context token budget."""

    async def _compact(
        self,
        ctx: EventContext,
        messages: list[Message],
        *,
        reason: str,
        context_tokens_before: int,
        estimate_source: str,
        request_estimate: int | None = None,
        context_limit: int | None = None,
        max_context_tokens: int | None = None,
        output_reservation: int | None = None,
        stable_prefix: Message | Sequence[Any] | None = None,
        removable_estimate: int | None = None,
    ) -> dict[str, Any] | None:
        """Build and execute a compaction proposal.

        Returns the proposal dict (with compact_reason, messages,
        compact_metrics) or None if no compaction needed.
        """

    async def _commit(
        self,
        ctx: EventContext,
        proposal: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Commit one proposal: emit PRE_COMPACT, verify, replace messages."""
```

### `CompactConfig` (`XBotv2/compact/config.py`)

```python
@dataclass(frozen=True, slots=True)
class CompactConfig:
    automatic: bool = True
    output_reservation: int | None = None
    trigger_ratio: float = 0.8
    keep_recent_turns: int = 4
    summary_max_chars: int = 8_000
```

`trigger_ratio` is the fraction of context window that triggers
compaction (default 80%). `keep_recent_turns` is the number of
recent turns always preserved. `summary_max_chars` caps the summary
text size.

### `parse_compact_config`

```python
def parse_compact_config(config: Any = None) -> CompactConfig:
    raw = dict(config or {})
    reservation = raw.get("output_reservation")
    return CompactConfig(
        automatic=bool(raw.get("automatic", True)),
        output_reservation=_integer(reservation, "output_reservation", minimum=0)
            if reservation is not None else None,
        trigger_ratio=_ratio(raw.get("trigger_ratio", 0.8)),
        keep_recent_turns=_integer(raw.get("keep_recent_turns", 4),
                                   "keep_recent_turns", minimum=1),
        summary_max_chars=_integer(raw.get("summary_max_chars", 8_000),
                                   "summary_max_chars", minimum=1),
    )
```

`_ratio` validates `0 < value <= 1.0`; `_integer` validates
`value >= minimum` and rejects booleans.

### `CompactEventsPort` / `UsagePort`

```python
class CompactEventsPort(Protocol):
    async def serial(self, event: str, *args: object) -> object: ...
    async def emit(self, event: str, *args: object) -> None: ...

class UsagePort(Protocol):
    async def add(self, usage: dict[str, object], *,
                  update_context: bool = True) -> dict[str, int] | None: ...
    async def update_context(self, context_tokens: int) -> dict[str, int]: ...
```

### `BeforeCompact` / `AfterCompact`

```python
@dataclass(frozen=True, slots=True)
class BeforeCompact:
    messages: list[Message]
    session: SessionInfo | None
    reason: str

@dataclass(frozen=True, slots=True)
class AfterCompact:
    messages: tuple[Message, ...]
    session: SessionInfo | None
    reason: str
    metrics: dict[str, Any]
    previous_message_count: int
    current_message_count: int

PRE_COMPACT = "pre/compact"
POST_COMPACT = "post/compact"
```

`BeforeCompact` is dispatched via `ctx.serial` — the first
non-`None` return can reject compaction by returning an error dict.
If rejected, the commit short-circuits with
`{"event": {"type": "error", "data": {"code": "hook_rejected", ...}}, "turn_complete": True}`.

## Compaction commit semantics

`_commit()` enforces strict invariants:

1. `PRE_COMPACT` is fired with `ctx.serial`. The handler may modify
   `pre.messages` (the summary replacement) but **may not change the
   retained tail**.
2. After `PRE_COMPACT`, `replacement = messages[:len(messages) - len(retained)]`
   where `retained = original_messages[prefix_end:]`. The check:
   ```python
   if retained and (
       len(messages) < len(retained)
       or messages[-len(retained):] != retained
   ):
       raise RuntimeError("PRE_COMPACT may only change the summary replacement.")
   ```
3. `self.state.replace_message_range(0, prefix_end, replacement,
   operation=f"compact:{compaction_id}", preserve_transcript=True)`
   commits the change to the history.
4. `POST_COMPACT` is emitted (observer), then `HISTORY_CHANGED` is
   emitted.
5. Usage is updated via `self._usage.update_context(...)`.

## Tool — `compact`

```python
def build_compact_tool(owner: _CompactToolOwner) -> Tool:
    async def request_compaction() -> ToolResult:
        owner.request_manual_compaction()
        return ToolResult.success("Conversation compaction requested.")
    return Tool.from_function(request_compaction, name="compact")
```

The tool does **not** perform compaction directly; it sets
`_manual_requested = True`. The next `BEFORE_CONTEXT` dispatch
consumes the flag and triggers `_compact()`.

## Command — `/compact`

```python
async def run_compact_command(
    service: CompactService, raw_args: str
) -> CommandResult: ...
```

Executes `_compact_current_history()` synchronously (within the
command handler). Does not require waiting for the next turn.

## How `apply()` works

```python
def apply(self, ctx, config):
    service = CompactService(
        events=ctx, model=ctx.model, state=ctx.loop_state,
        usage=ctx.usage, config=parse_compact_config(config),
    )
    ctx.dispose(service._dispose)
    ctx.on(Events.BEFORE_CONTEXT, service._on_before_context)
    ctx.on(Events.BEFORE_MODEL_REQUEST, service._on_before_model_request)
    ctx.tools.register(build_compact_tool(service))
    ctx.commands.register(Command(
        name="compact",
        description="Compact conversation history immediately while idle.",
        handler=service._compact_command,
        usage="/compact",
        examples=("/compact",),
    ))
    ctx.set("compact", service)
```

## On-disk artifacts

Compaction records are written to the history via
`self.state.history.record("compaction/summary", {...})` which
appends to `messages.jsonl` as a `TrajectoryEventRecord`:

```json
{"schema_version": 1, "record_type": "event",
 "data": {"compaction_id": "...", "reason": "...",
          "summary": "...", "raw_output": "...",
          "source_node_ids": [...], "provider": "...",
          "model": "...", "usage": {...}, "metrics": {...}},
 "position": N}
```

## Cross-references

- Depends on: `tools`, `commands`, `model`, `loop_state`, `usage`,
  `agentloop` (subscribes to `BEFORE_CONTEXT`, `BEFORE_MODEL_REQUEST`).
- Depended on by: the Agent (tool call or slash command).
- Pairs with: `persistence` (history owner), `llm` (summary generation).

## Common pitfalls

- **Calling `compact` tool repeatedly**: if automatic compaction is
  active, manual requests are consumed on the next turn — redundant
  calls are no-ops until the compaction completes.
- **Expecting `BeforeCompact` to add new messages**: `PRE_COMPACT`
  may only modify the summary replacement text, not change message
  count or append new content.
- **Mutating `CompactService._manual_requested` directly**: use
  `request_manual_compaction()` which sets it atomically.
- **Assuming `trigger_ratio=0.8` means 80% of output tokens**: it's
  80% of `context_window * (1 - output_reservation / context_window)`.
  The formula accounts for output reservation.
- **Overriding `keep_recent_turns` with `0`**: validation requires
  `>= 1`. The default `4` is conservative for most use cases.
