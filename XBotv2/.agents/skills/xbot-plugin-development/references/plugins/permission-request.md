# `permission-request`

The live one-shot approval channel for tool `ask` decisions. Provides
`ctx.approval` — transport for permission requests that delegates to
the client event protocol or falls back to the in-memory waiter.

- **Import/profile:** `permission-request`, Agent profile.
- **Source:** `XBotv2/permission_request/plugin.py`,
  `XBotv2/permission_request/service.py`,
  `XBotv2/permission_request/protocol.py`.
- **Injects/provides:** `client_events` → `approval` (`ApprovalService`).
- **Subscribes to events:** `session/close` (cancel all waiters).
- **Emits:** `client/event` (permission request/response).

## Public data models

### `ApprovalService` (`XBotv2/permission_request/service.py:16-40`)

```python
class ApprovalService:
    """Live approval transport with no permission-policy knowledge."""

    def __init__(
        self,
        events: ApplicationEventsPort,
        client_events: ClientEventsPort,
    ) -> None:
        self._events = events
        self._client_events = client_events
        self._waiter = InteractionWaiter()

    @property
    def waiter(self) -> InteractionWaiter: ...

    def session_closed(self, _event: EventContext) -> None:
        self._waiter.cancel_all("session_closed")

    async def request(
        self, client_event: ClientEvent
    ) -> dict[str, JsonValue]:
        """Publish a request and return the client's raw decision record."""
```

### `InteractionWaiter` (`XBotv2/interactions/interactions.py`)

```python
class InteractionWaiter:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[InteractionResult]] = {}

    def register(self, request_id: str) -> asyncio.Future[InteractionResult]: ...
    async def wait(self, request_id: str, timeout_seconds: float | None) -> InteractionResult: ...
    def answer(self, request_id: str, *, answer: Any = None,
               decision: str = "", scope: str = "once") -> InteractionResult: ...
    def cancel(self, request_id: str, reason: str = "cancelled") -> InteractionResult: ...
    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResult]: ...
    def pending_request_ids(self) -> list[str]: ...

class InteractionResult(BaseModel):
    request_id: str
    status: str          # "answered", "timeout", "cancelled"
    answer: Any = None
    decision: str = ""
    scope: str = "once"
    reason: str = ""
```

### `PermissionRequestData` / `PermissionResponseRequest`

```python
class PermissionRequestData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any] | None = None
    permission: RequestedPermissionData | None = None
    decision: Literal["ask"] = "ask"
    reason: str
    resume_supported: bool = False

    @model_validator(mode="after")
    def _require_subject(self) -> "PermissionRequestData":
        """Requires exactly one of tool_call or permission."""

class PermissionResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    scope: Literal["once", "session"] = "once"

class RequestedPermissionData(WireModel):
    tool: str = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict)

class PermissionDeniedData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any]
    decision: Literal["deny"] = "deny"
    reason: str
    resume_supported: bool = False
```

## How `apply()` works

```python
def apply(self, ctx, config=None):
    service = ApprovalService(ctx, ctx.client_events)
    ctx.set("approval", service)
    ctx.dispose(ctx.client_events.register_waiter(
        "permission_request", service.waiter
    ))
    ctx.on(Events.SESSION_CLOSE, service.session_closed)
```

The `ApprovalService` registers its `InteractionWaiter` as the sink
for `permission_request` client events. When the `permissions` plugin's
guard encounters an `ask` decision, it emits a `ClientEvent` via
`ApprovalService.request()`.

## Request flow

```
permission guard → ask decision → emit ClientEvent →
  client event sink (preferred) OR
  ApprovalService.waiter (fallback) →
  client responds → waiter resolves →
  request() returns {"status": "answered", "decision": "allow", ...}
```

## Typical extension: register an answerer

```python
from XBotv2.client_events import ClientEventsPort
from XBotv2.core.tools import ClientEvent

class MyClientPlugin:
    inject = ["client_events"]

    def apply(self, ctx, config):
        async def answer(event: ClientEvent) -> dict | None:
            if event.type == "permission_request":
                # ... handle and return decision
                return {"decision": "allow", "scope": "once"}
            return None
        ctx.client_events.register_answerer("permission_request", answer)
```

## Cross-references

- Depends on: `client_events`, `agentloop` (`SESSION_CLOSE`),
  `interactions` (`InteractionWaiter`).
- Depended on by: `permissions` (delegates ask decisions here),
  the permissions guard pipeline.
- Pairs with: `permissions` (the policy decision source),
  `interactions` (shared `InteractionWaiter` pattern).

## Common pitfalls

- **`request()` without a client event sink**: falls back to the
  `InteractionWaiter` which blocks indefinitely unless a timeout
  is set. The `interaction` plugin handles timeouts for
  `ask_user`; this plugin does not — always install a client
  answerer.
- **`PermissionRequestData` requires exactly one of `tool_call` or
  `permission`**: having both or neither raises a `ValueError`.
- **`scope="once"` by default**: `PermissionResponseRequest.scope`
  defaults to `"once"`; use `"session"` for persistent grants.
- **`session_closed` cancels all waiters**: if a permission request
  is pending when the session closes, it resolves with
  `status="cancelled", reason="session_closed"`.
- **`ctx.approval` is the `ApprovalService`, not the guard**: the
  guard is owned by the `permissions` plugin (`PermissionGuard`).
  `approval` is the transport layer only.
