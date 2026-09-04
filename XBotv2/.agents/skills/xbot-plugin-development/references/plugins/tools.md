# `tools`

The standard Tool registry and executor. Every Agent-visible work
request goes through this plugin's pipeline:
`BEFORE_TOOL_CALL` rewrite → schema validation → monotonic guards →
permissions → dispatch → `AFTER_TOOL_CALL`. A Tool that bypasses this
path loses permissions, sandboxing, schema checks, and event observers.

- **Import/profile:** tree id `tools`, import name `agentloop.tools`,
  Agent profile.
- **Source:** `XBotv2/agentloop/tools/plugin.py`,
  `XBotv2/agentloop/tool_service.py`, `XBotv2/agentloop/tool_registry.py`,
  `XBotv2/core/tools.py`.
- **Injects/provides:** `runtime_log` → `tools` (`ToolsService`).
- **Subscribes to events:** consumes `before/tool-call` (args rewrite)
  and emits `after/tool-call` / `tool/call-failure` / `tool/denied`.

## Public data models (`XBotv2/core/tools.py`)

### `ToolCall` — what the model emits

```python
class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    type: Literal["tool_call"] = "tool_call"
```

### `ToolResult` — what the handler returns

```python
class ToolResult(BaseModel):
    status: Literal["success", "error", "denied", "cancelled"] = "success"
    content: str = ""
    data: JsonValue = None
    error: ToolError | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    images: tuple[ImageContent, ...] = ()
    client_events: tuple[ClientEvent, ...] = ()
    turn_complete: bool = False

    @classmethod
    def success(
        cls,
        content: str = "",
        *,
        data: JsonValue = None,
        artifacts: tuple[ArtifactRef, ...] = (),
        images: tuple[ImageContent, ...] = (),
        client_events: tuple[ClientEvent, ...] = (),
        turn_complete: bool = False,
    ) -> "ToolResult": ...

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JsonValue] | None = None,
    ) -> "ToolResult": ...
```

`status` drives the next turn decision. `artifacts` and `images` are
session-relative references; the model sees them as such. `data` and
`client_events` must be JSON-compatible.

### `ToolError`

```python
class ToolError(BaseModel):
    code: str                           # stable identifier
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)
```

### `GuardDecision`

```python
@dataclass
class GuardDecision:
    action: Literal["deny"] = "deny"
    reason: str = ""
    source: str = "guard"
    client_events: tuple[ClientEvent, ...] = ()
```

Return from `ToolGuard.allow(call) -> GuardDecision | None`. `None`
means "no opinion" — the chain continues.

### `Tool`

```python
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]        # JSON Schema fragment
    tool_call_parameter: str | None = None
    namespace: str | None = None
    timeout_seconds: float | None = None

    @classmethod
    def from_function(
        cls,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        tool_call_parameter: str | None = "tool_call",
    ) -> "Tool": ...
```

`from_function` builds the JSON Schema from the callable's signature.
**All keyword-only parameters that are not `tool_call` MUST be
constructor-injected, not signature-injected.**

`tool_call_parameter` (default `"tool_call"`):
- If set, the callable may declare a keyword-only
  `tool_call: ToolCall` parameter; the engine passes the rewritten
  call after `BEFORE_TOOL_CALL`. The parameter is omitted from the
  provider schema.
- Set to `None` to omit entirely.

### `ClientEvent` (subset used by Tools)

```python
class ClientEvent(BaseModel):
    type: str = Field(min_length=1)
    data: dict[str, JsonValue] = Field(default_factory=dict)
```

## `ToolsService` (`agentloop/tool_service.py:35-170`)

```python
class ToolsService:
    def register(
        self,
        tool: Tool,
        *,
        namespace: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str: ...                              # returns registration name

    def unregister(self, name: str) -> bool: ...

    def resolve(
        self, name: str, *, include_disabled: bool = False
    ) -> Tool | None: ...

    def names(self) -> tuple[str, ...]: ...
    def registered_names(self) -> tuple[str, ...]: ...
    def enabled(self) -> tuple[Tool, ...]: ...
    def registrations(self) -> tuple[ToolRegistration, ...]: ...

    def restrict(
        self, selectors: list[str] | None
    ) -> tuple[str, ...]: ...                  # limit which Tools the model sees

    def exclude(self, selectors: list[str]) -> tuple[str, ...]: ...

    def guard(self, guard: ToolGuard) -> object: ...   # returns disposer
    def guards(self) -> tuple[ToolGuard, ...]: ...
```

Registration is **fiber-owned**: `ctx.tools.register(...)` ties cleanup
to the current XCore fiber and unregisters automatically on unload.
A guard added with `guard(...)` returns a disposer for explicit removal.

## `ToolGuard` Protocol

```python
class ToolGuard(Protocol):
    def allow(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_call: ToolCall | None = None,
    ) -> GuardDecision | None: ...
```

Run before permission checks; short-circuits with `GuardDecision(action="deny")`.

## `ToolRegistration`

The internal record returned by `register(...)`:

```python
@dataclass(frozen=True)
class ToolRegistration:
    name: str                       # registration identity (uniquely scoped)
    tool: Tool
    namespace: str | None
    timeout_seconds: float | None
```

## Operations

| Operation | Purpose |
|---|---|
| `LIST_TOOLS` (`EmptyRequest → ToolCatalog`) | catalog for UI |
| `ToolsCatalogHandler.list_tools(request)` | implementation that walks `ToolsService` |

## Standard pipeline

```text
Events.BEFORE_TOOL_CALL (rewrite args) ──▶ schema validation ──▶
guards (ToolGuard.allow) ──▶ permission check ──▶ Tool dispatch ──▶
Events.AFTER_TOOL_CALL (success | failure | denied)
```

Short-circuit at any step replaces the dispatch:
- Returning a non-`None` value from a `BEFORE_TOOL_CALL` observer
  skips the call.
- A `GuardDecision(action="deny", ...)` short-circuits before
  permissions.
- `Events.TOOL_DENIED` is emitted when the permissions service denies.

## Typical extension: register a Tool

```python
from XBotv2.core import Tool, ToolResult

class WeatherHandler:
    def __init__(self, client):
        self._client = client

    async def weather(self, city: str) -> ToolResult:
        report = await self._client.current(city)
        return ToolResult.success(f"Weather loaded for {city}", data=report)


class WeatherPlugin:
    name = "weather"
    inject = ["tools", "weather_client"]

    def apply(self, ctx, config):
        handler = WeatherHandler(ctx.weather_client)
        ctx.tools.register(
            Tool.from_function(handler.weather, name="weather"),
            namespace="weather",
            timeout_seconds=30,
        )
```

`ToolResult.failure("rate_limited", "try again later",
retryable=True)` is the right shape for a domain failure.

## Cross-references

- Depends on: `runtime_log`.
- Depended on by: `permissions`, `coretools`, `subagents`,
  `mcp_plugin`, `browser`, `goal`, `todolist`, `interactions`,
  `content_cache`, `compact`, `skills`, every Tool-registering plugin.
- Pairs with: `permissions` (tool allow/deny), `sandbox` (path
  capability), `coretools` (built-in filesystem/shell tools),
  `subagents` (subagent-launching Tools).

## Common pitfalls

- **Capturing `Context` in the handler closure**: bind the narrow
  dependencies in the handler's `__init__`, not the whole ctx.
- **Returning a Tool that hides a service in its schema**: the
  service must be a constructor dependency of the handler, not a
  parameter on the registered function. The function signature *is*
  the model schema.
- **Skipping the registry for "trusted" Tools**: the registry owns
  schema validation, permissions, sandbox, and event observers. Call
  the handler directly and you lose all of them.
- **Persisting tool results in plugin state**: results belong in
  `ThreadPersistence.history` via `BEFORE_TOOL_CALL` rewriting or
  normal conversation flow; tool state goes through the message
  record, not a custom JSON file.
- **Using `ToolCall` from the wrong package**: it lives in
  `XBotv2.core`, not `XBotv2.agentloop`.
- **Forgetting `tool_call_parameter` for call identity**: a Tool
  that needs the final rewritten call (after `BEFORE_TOOL_CALL`)
  should declare a keyword-only `tool_call: ToolCall` parameter.
  Core omits it from the provider schema and passes the rewritten
  call.
