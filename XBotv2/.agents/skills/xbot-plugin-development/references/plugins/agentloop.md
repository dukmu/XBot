# `agentloop`

The Agent reasoning loop. Owns turn sequencing, dispatches every named
event on the XCore context, and constructs the `Engine` after all
launch facts are composed.

- **Import/profile:** tree id `agentloop`, import name `agentloop.runtime`,
  Agent profile.
- **Source:** `XBotv2/agentloop/runtime/plugin.py`,
  `XBotv2/agentloop/factory.py`, `XBotv2/agentloop/engine.py`,
  `XBotv2/agentloop/events.py`.
- **Injects/provides:** `runtime_log` → `agent_loop_factory`, which
  publishes the constructed loop as `ctx.engine`.
- **Subscribes to events:** none in `apply`; the loop *emits* events.
- **Emits:** every `Events.*` name — see
  [../events-catalog.md](../events-catalog.md).

## Public data models

### `Events` (`XBotv2/agentloop/events.py:17-69`)

```python
class Events:
    # Session lifecycle
    SESSION_START = "session/start"
    SESSION_RESUME = "session/resume"
    SESSION_CLOSE = "session/close"
    # Turn lifecycle
    TURN_START = "turn/start"
    TURN_END = "turn/end"
    ON_ERROR = "error"
    ON_STOP = "stop"
    ON_STOP_FAILURE = "stop/failure"
    # User input
    BEFORE_USER_MESSAGE_ACCEPT = "before/user-message-accept"
    AFTER_USER_MESSAGE_ACCEPT = "after/user-message-accept"
    # Context building
    BEFORE_CONTEXT = "before/context"
    AFTER_CONTEXT = "after/context"
    # Agent / model
    BEFORE_AGENT = "before/agent"
    BEFORE_TOOL_SCHEMA_BIND = "before/tool-schema-bind"
    AFTER_TOOL_SCHEMA_BIND = "after/tool-schema-bind"
    BEFORE_MODEL_REQUEST = "before/model-request"
    MODEL_REQUEST_READY = "model/request-ready"
    AFTER_MODEL_RESPONSE = "after/model-response"
    MODEL_REQUEST_ERROR = "model/request-error"
    AFTER_AGENT = "after/agent"
    # Tools
    BEFORE_TOOLS = "before/tools"
    AFTER_TOOLS = "after/tools"
    INBOX_SPLICE = "agent/inbox/spliced"
    TOOL_CALLS_PARSED = "tool/calls-parsed"
    BEFORE_TOOL_CALL = "before/tool-call"
    AFTER_TOOL_CALL = "after/tool-call"
    TOOL_CALL_FAILURE = "tool/call-failure"
    TOOL_DENIED = "tool/denied"
    POST_TOOL_BATCH = "tool/batch-done"
    # Messages
    USER_MESSAGE = "user/message"
    ASSISTANT_MESSAGE = "assistant/message"
    TOOL_MESSAGE = "tool/message"
    # Permissions / client
    CLIENT_EVENT = "client/event"
    STATE_CHANGED = "state/changed"
```

### `SHORT_CIRCUIT_EVENTS` (`events.py:71-82`)

```python
SHORT_CIRCUIT_EVENTS = frozenset({
    Events.BEFORE_USER_MESSAGE_ACCEPT,
    Events.BEFORE_CONTEXT,
    Events.AFTER_CONTEXT,
    Events.BEFORE_MODEL_REQUEST,
    Events.BEFORE_AGENT,
    Events.BEFORE_TOOL_SCHEMA_BIND,
    Events.AFTER_AGENT,
    Events.BEFORE_TOOLS,
    Events.BEFORE_TOOL_CALL,
    Events.AFTER_TOOLS,
})
```

These dispatch via `ctx.serial`; first non-`None` return wins. Others
dispatch via `ctx.emit` and observers must return `None`.

### `EventContext` (`events.py:90-117`)

```python
@dataclass
class EventContext:
    messages: Sequence[Message] = ()
    settings: LoopSettings | None = None
    continuation: bool = False
    session: SessionInfo | None = None         # not the Session object
    user_input: str | None = None
    turn_complete: bool = False
    context_messages: list[Message] | None = None
    agent_response: ModelResponse | None = None
    model_request: ModelRequest | None = None
    model_response: ModelResponse | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call: ToolCall | None = None          # singular, current call
    args: dict[str, Any] | None = None        # current call's args
    tool_result: Message | None = None
    tool_results: list[Message] | None = None
    error: BaseException | None = None
    rebuild: bool = False
    client_event: ClientEvent | None = None
    stop_reason: str | None = None
    request_id: str = ""
```

### `LoopSettings` / `LoopState` / `ModelRequest` / `ModelResponse`

All in `XBotv2/agentloop/contracts.py`:

```python
@dataclass
class LoopSettings:
    model: str
    provider: str
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    # ... plus capability flags the loop honors

@dataclass
class LoopState:
    session: SessionInfo
    history: ConversationHistory
    inbox_items: list[InboxItem]
    metadata: ThreadMetadataState
    resumed: bool
    # ... plus runtime counters

@dataclass
class ModelRequest:
    messages: list[Message]
    tools: list[Tool]
    settings: LoopSettings
    # ...

@dataclass
class ModelResponse:
    message: Message | None
    usage: UsageData | None
    finish_reason: str | None
    # ...
```

## `EventPort` consumer Protocol

```python
class EventPort(Protocol):
    async def emit(self, event: str, *args: Any) -> Any: ...
    async def serial(self, event: str, *args: Any) -> Any: ...
```

A driver (test harness or alternate loop) implements this to receive
events without depending on the full XBot composition.

## Extension contract

There are exactly three legitimate surfaces:

1. **Observe** — `ctx.on(Events.X, handler)` returning `None`. Works
   for every event.
2. **Short-circuit** — only for `SHORT_CIRCUIT_EVENTS`. Return a
   non-`None` value to override the engine's default; first wins.
3. **Replace** — provide a custom `agent_loop_factory` (advanced; only
   when the bundled loop cannot satisfy the composition).

Do **not** monkey-patch `Engine`, subscribe to a private `_on_*` method,
or synthesize an `EventContext` for cross-plugin facts — publish a
typed event from your owning package.

## Typical extension

```python
from XBotv2.agentloop import Events, EventContext
from xcore import S

class MetricsPlugin:
    name = "metrics"
    inject = ["runtime_paths", "state"]
    Config = S.object({"enabled": S.boolean().default(True)}).strict()

    def apply(self, ctx, config):
        store = ctx.state.namespace("metrics")
        ctx.on(Events.AFTER_MODEL_RESPONSE, self._record)
        ctx.on(Events.STATE_CHANGED, self._snapshot)

    async def _record(self, event: EventContext) -> None:
        usage = event.model_response.usage if event.model_response else None
        if usage:
            ...

    async def _snapshot(self, event: EventContext) -> None:
        # observe the persisted-state projection change
        ...
```

## Short-circuit pattern

```python
async def require_user_ack(event: EventContext):
    if event.user_input and "dangerous" in event.user_input.lower():
        return {"reject": True, "reason": "policy: needs explicit approval"}
    return None  # let normal handling continue

ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, require_user_ack)
```

A non-`None` return for a short-circuit event replaces the engine's
default. `None` means "no opinion" — the next observer or default
takes over.

## On-disk artifacts

None directly. The loop is runtime-only; conversation persistence and
usage accounting are owned by [persistence.md](persistence.md) and
[usage.md](usage.md).

## Cross-references

- Depends on: `runtime_log`.
- Depended on by: every plugin that observes events; `tools`,
  `permissions`, `coretools`, `compact`, `usage`, `persistence`,
  `token_manager` all listen on `Events.*`.
- Pairs with: [agent-runtime.md](agent-runtime.md) (composition that
  wires selection + loop), [session.md](session.md) (per-thread
  identity for events).

## Common pitfalls

- **Returning a value for a non-short-circuit event**: observer events
  use `ctx.emit`; non-`None` returns are ignored.
- **Importing `EventContext` from the wrong module**: it lives in
  `XBotv2.agentloop`, not `XBotv2.core`.
- **Reading `event.session` for cross-session facts**: `EventContext.session`
  is the *current thread's* `SessionInfo`; use `ctx.sessions`
  (process manager) for cross-session data.
- **Coupling to internal `Engine` method names**: those are not part of
  the public contract; subscribe to events instead.
- **Inventing event names for cross-plugin facts**: define a typed
  contract in the owning package's `contracts.py` and emit that.
