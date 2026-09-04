# `usage`

Cumulative model usage tracking stored through the shared `StateService`
protocol. Owns one `UsageData` snapshot per thread that persists across
turns.

- **Import/profile:** `usage`, Agent profile.
- **Source:** `XBotv2/usage/plugin.py`.
- **Injects/provides:** `state`, `loop_state`, `runtime_log` → `usage`
  (`UsageService`).
- **Subscribes to events:** `application/initialized` (initialize from
  history, `prepend=True`), `after/model-response` (accumulate deltas).

## Public data models

### `UsageService` (`XBotv2/usage/plugin.py:17-89`)

```python
class UsageService:
    """Own the one cumulative usage snapshot for a thread."""

    def __init__(
        self,
        store: StateService,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._store = store
        self._log = runtime_log.bind("usage")
        self._snapshot = UsageData()
        self._initialized = False

    async def initialize(self, messages: Sequence[Message]) -> None:
        """Seed usage from history messages or a persisted snapshot.

        Sets `self._initialized = True`. Subsequent calls are no-ops.
        """

    def snapshot(self) -> UsageData:
        """Return the current cumulative usage snapshot."""

    async def add(
        self,
        usage: Mapping[str, object],
        *,
        update_context: bool = True,
    ) -> dict[str, int] | None:
        """Add a delta (from a model response).

        If `delta.is_empty()` returns `None` and skips persistence.
        When `update_context=False`, context_tokens are not updated.
        """

    async def update_context(self, context_tokens: int) -> dict[str, int]:
        """Persist and publish a new effective-context size without a request."""
```

### `UsageData` (`XBotv2/core/usage.py`)

```python
class UsageData(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    context_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    prompt_cache_write_tokens: int = Field(default=0, ge=0)

    @classmethod
    def from_provider(cls, usage: Mapping[str, object]) -> "UsageData": ...

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "UsageData": ...

    def to_snapshot(self) -> dict[str, Any]: ...

    def add(self, delta: "UsageData") -> "UsageData": ...

    def is_empty(self) -> bool: ...

    def to_event_dict(self) -> dict[str, int]: ...
```

### `UsageHandlers` (`XBotv2/usage/plugin.py:92-103`)

```python
class UsageHandlers:
    def __init__(self, service: UsageService, state: LoopState) -> None:
        self._service = service
        self._state = state

    async def initialize(self, _event: ApplicationInitialized) -> None:
        await self._service.initialize(self._state.messages)

    async def record(self, event: EventContext) -> None:
        response = event.model_response
        if response is not None and response.usage_metadata:
            await self._service.add(response.usage_metadata)
```

## How `apply()` works (`UsageComponent`)

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    service = UsageService(ctx.state.namespace("usage"), ctx.runtime_log)
    handlers = UsageHandlers(service, ctx.loop_state)
    ctx.set("usage", service)
    ctx.on(Events.AFTER_MODEL_RESPONSE, handlers.record)
    ctx.on(APPLICATION_INITIALIZED, handlers.initialize, prepend=True)
```

`prepend=True` ensures `initialize` fires before `after/model-response`
on the first turn.

## On-disk artifacts

`UsageService` uses `ctx.state.namespace("usage")` — the XCore
`StateService` for the thread. The snapshot is persisted under the key
`"snapshot"` as a plain dict:

```json
{"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
 "requests": 1, "context_tokens": 1500}
```

## Typical extension: read cumulative usage

```python
class UsageAwarePlugin:
    inject = ["usage"]

    def apply(self, ctx, config):
        snap = ctx.usage.snapshot()
        # snap is UsageData — read snap.input_tokens, snap.requests, etc.
        ...
```

## Cross-references

- Depends on: `state`, `loop_state`, `runtime_log`, `agentloop`
  (subscribes to `AFTER_MODEL_RESPONSE`, `APPLICATION_INITIALIZED`).
- Depended on by: `compact` (reads context_tokens for budgeting),
  `token-manager` (reads context_tokens), `usage` UI display.
- Pairs with: `llm` (the response `usage_metadata` is the delta source).

## Common pitfalls

- **Calling `add()` before `initialize()`**: raises `RuntimeError`.
  The service must be initialized from history first.
- **Passing `update_context=False`**: context_tokens are preserved
  from the existing snapshot; useful when a non-request update is
  needed (e.g., context-building changes the effective size).
- **Expecting `UsageData.from_provider` to handle unknown keys**: it
  only reads known fields; unknown keys are silently dropped.
