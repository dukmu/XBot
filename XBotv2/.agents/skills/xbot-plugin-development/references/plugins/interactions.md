# `interactions`

Live client interaction coordination — manages model-facing
`ask_user` input requests, `send_message` notifications, and the
in-memory `InteractionWaiter` per engine turn.

- **Import/profile:** `interactions`, Agent profile.
- **Source:** `XBotv2/interactions/plugin.py`,
  `XBotv2/interactions/interactions.py`,
  `XBotv2/interactions/tools.py`,
  `XBotv2/interactions/protocol.py`.
- **Injects/provides:** `tools`, `client_events`,
  `session_launch` → `interactions` (`InteractionsService`).
- **Subscribes to events:** `session/close` (cancel all waiters).
- **Emits:** `client/event` (`ClientEvent` for `user_input_required`,
  `client_message`, `interaction_recorded`).
- **Tools:** `ask_user`, `send_message`.

## Public data models

### `InteractionsService` (`XBotv2/interactions/plugin.py:23-80`)

```python
class InteractionsService:
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

    async def request_user_input(
        self,
        question: str,
        *,
        options: list[dict[str, str]] | None = None,
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """Publish and resolve one user-input request.

        Routes through the installed live sink (preferred) or the
        fallback waiter. Without a live sink the request fails to
        ``unsupported`` so the turn never hangs.
        """
```

### `InteractionWaiter` (`XBotv2/interactions/interactions.py:22-105`)

```python
class InteractionWaiter:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[InteractionResult]] = {}

    def register(self, request_id: str) -> asyncio.Future[InteractionResult]: ...

    async def wait(
        self, request_id: str, timeout_seconds: float | None
    ) -> InteractionResult: ...

    async def wait_registered(
        self,
        request_id: str,
        future: asyncio.Future[InteractionResult],
        timeout_seconds: float | None,
    ) -> InteractionResult: ...

    def answer(
        self, request_id: str, *, answer: Any = None,
        decision: str = "", scope: str = "once"
    ) -> InteractionResult: ...

    def cancel(self, request_id: str, reason: str = "cancelled") -> InteractionResult: ...

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResult]: ...

    def pending_request_ids(self) -> list[str]: ...

class InteractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    status: str                       # "answered", "timeout", "cancelled"
    answer: Any = None
    decision: str = ""
    scope: str = "once"
    reason: str = ""
```

### `UserInputRequiredData` / `UserInputOption` / `InteractionRecordedData`

```python
class UserInputOption(WireModel):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)

class UserInputRequiredData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[UserInputOption] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_supported: bool = False

class UserInputResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    answer: Any = None

class InteractionRecordedData(WireModel):
    request_id: str = Field(min_length=1)
    status: Literal["answered", "timeout", "cancelled"]
    decision: Literal["allow", "deny", ""] = ""
    scope: Literal["once", "session", ""] = ""
    answer: Any = None
    pending_interactions: list[str] = Field(default_factory=list)

class InteractionResponse(WireModel):
    request_id: str = Field(min_length=1)
    recorded: Literal[True] = True
    pending_interactions: list[str] = Field(default_factory=list)

InteractionEventType = Literal[
    "permission_response_recorded",
    "user_input_recorded",
]
```

### `ClientMessageData` / `ClientEvent`

```python
class ClientMessageData(WireModel):
    message: str = Field(min_length=1)
    level: Literal["info", "warning", "error"] = "info"
    source: str = Field(min_length=1)
    tool_call_id: str = ""
```

### `send_message` Tool

```python
send_message = Tool.from_function(
    send_message_to_user,
    name="send_message",
)

def send_message_to_user(
    message: str,
    level: Literal["info", "warning", "error"] = "info",
) -> ToolResult:
    """Send a non-blocking progress or diagnostic message to the client."""
    return ToolResult(
        content=f"Message sent to user: {message}",
        client_events=(ClientEvent(
            type="client_message",
            data=ClientMessageData(message=message, level=level,
                                  source="send_message").model_dump(),
        ),),
    )
```

### `ask_user` Tool

```python
def build_ask_user_tool(interactions: Any) -> Tool:
    async def invoke(
        question: str,
        options: list[dict[str, str]],
        timeout_seconds: float | None = None,
        *,
        tool_call: ToolCall,
    ) -> ToolResult: ...

    return Tool(
        name="ask_user",
        description=...,
        function=invoke,
        parameters=_ASK_USER_SCHEMA,
        tool_call_parameter="tool_call",
    )
```

`_ASK_USER_SCHEMA`:

```python
{
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "question": {"type": "string", "minLength": 1},
        "options": {
            "type": "array", "minItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
                "required": ["label", "description"],
            },
        },
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": ["question", "options"],
}
```

## How `apply()` works

```python
def apply(self, ctx, config=None):
    config = config or {}
    service = InteractionsService(ctx, ctx.client_events)
    ctx.set("interactions", service)
    ctx.dispose(ctx.client_events.register_waiter(
        "user_input_required", service.waiter
    ))
    ctx.tools.register(send_message)
    if ctx.session_launch.interactive:
        ctx.tools.register(build_ask_user_tool(service))
    ctx.on(Events.SESSION_CLOSE, service.session_closed)
```

`ask_user` is only registered in **interactive** sessions
(`ctx.session_launch.interactive`). Non-interactive sessions get
`send_message` only.

## Interaction flow

```
ask_user() → interactions.request_user_input() →
  emit CLIENT_EVENT with user_input_required →
  route through live client sink OR
  InteractionWaiter.wait(request_id, timeout) →
  client responds → future.set_result() →
  ask_user returns ToolResult.success(answer)
```

## Cross-references

- Depends on: `tools`, `client_events`, `session_launch`,
  `agentloop` (`SESSION_CLOSE`).
- Depended on by: `permission_request` (uses `InteractionsService`
  for approval flow), the Agent (`ask_user` / `send_message` tools).
- Pairs with: `permission-request` (interactive approval),
  `session` (interactive flag).

## Common pitfalls

- **`ask_user` not available in non-interactive sessions**: only
  registered when `ctx.session_launch.interactive` is True.
- **`options` requires at least 2 items**: `_ASK_USER_SCHEMA`
  validates `minItems=2`. One-option prompts will fail schema
  validation.
- **`timeout_seconds` defaults to None → `unsupported`**: without
  a live client sink and without `timeout_seconds`, the waiter
  returns `"unsupported"` immediately. Always set `timeout_seconds`
  when the client may not respond.
- **`InteractionWaiter.register()` raises on duplicate**: if the
  same `request_id` is registered twice, `InteractionNotPending`
  is raised. Use unique IDs per request.
- **`send_message` is non-blocking**: it emits a `ClientEvent`
  but does not wait for delivery. Use `ask_user` when a response
  is required.
- **`session_closed` cancels all waiters**: if a session is closed
  while a waiter is active, all pending interactions resolve to
  `status="cancelled", reason="session_closed"`.
