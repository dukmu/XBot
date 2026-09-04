# `server-routes-session`

Session, thread, message, history, fork, event-stream, and interaction
routes. Registered via `contribute_router()` as `xbot.http.session`.

- **Import/profile:** `server-routes-session`, server profile.
- **Source:** `XBotv2/session/protocol.py`,
  `XBotv2/session/http/plugin.py`.
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Router registration (`XBotv2/session/http/plugin.py`)

```python
class SessionHttpPlugin:
    name = "xbot.http.session"
    inject = ["server", "sessions", "server_options", "workspace_events"]

    async def apply(self, ctx, config=None):
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_session_router(
                sessions=ctx.sessions,
                options=ctx.server_options,
                workspace_events=ctx.workspace_events,
            ),
            exception_handlers=(
                (SessionNotFound, _session_not_found),
                (ThreadNotActive, _thread_not_active),
            ),
        )
```

## Routes (`build_session_router`) — `XBotv2/session/protocol.py`

```python
def build_session_router(
    *,
    sessions: SessionsPort,
    options: ServerOptions,
    workspace_events: WorkspaceEventCursor,
) -> APIRouter:
```

### `POST /sessions` → `OpenSessionResponse`

```python
@router.post("/sessions", operation_id="open_session")
async def open_session(
    payload: OpenSessionRequest,
    llm_override: ModelOverride,
) -> OpenSessionResponse:
```

Creates a new session via `sessions.open(OpenSession(...))`.

```python
class OpenSessionRequest(WireModel):
    session_id: str | None = None
    thread_id: str = "agent"
    workspace_root: str | None = None
    mode: SessionMode = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)
```

```python
class OpenSessionResponse(SessionDescriptor):
    status: Literal["ready"] = "ready"
    history: list[SessionHistoryItem] = Field(default_factory=list)
    history_cursor: str | None = None
    pending_inputs: list[PendingInputData] = Field(default_factory=list)
```

### `GET /sessions` → `SessionListResponse`

```python
@router.get("/sessions", operation_id="list_sessions")
async def list_sessions(
    request: Request,
    event_cursor: int | None = None,
) -> SessionListResponse:
```

Lists sessions from `sessions.list_sessions()`. Filters by workspace
if `ServerOptions.workspace_root` is set. Returns `event_cursor` for
SSE subscription.

### `GET /sessions/{session_id}` → `SessionSummary`

```python
@router.get("/sessions/{session_id}", operation_id="session_info")
async def session_info(session_id: str) -> SessionSummary:
```

### `PATCH /sessions/{session_id}` → `SessionSummary`

```python
@router.patch("/sessions/{session_id}", operation_id="rename_session")
async def rename_session(
    session_id: str,
    payload: SessionUpdateRequest,
) -> SessionSummary:
```

### `POST /sessions/{session_id}/fork` → `ForkResponse`

```python
@router.post("/sessions/{session_id}/fork", operation_id="fork_session")
async def fork_session(session_id: str) -> ForkResponse:
```

### `DELETE /sessions/{session_id}` → `DeleteSessionResponse`

```python
@router.delete("/sessions/{session_id}", operation_id="delete_session")
async def delete_session(session_id: str) -> DeleteSessionResponse:
```

### `GET /sessions/{session_id}/threads` → `ThreadListResponse`

```python
@router.get("/sessions/{session_id}/threads", operation_id="list_threads")
async def list_threads(session_id: str) -> ThreadListResponse:
```

### `POST /sessions/{session_id}/threads` → `OpenThreadResponse`

```python
@router.post("/sessions/{session_id}/threads", operation_id="open_thread")
async def open_thread(
    session_id: str,
    payload: OpenThreadRequest,
) -> OpenSessionResponse:
```

### `GET /sessions/{session_id}/threads/{thread_id}` → `ThreadSummary`

```python
@router.get("/sessions/{session_id}/threads/{thread_id}", ...)
```

### `GET /sessions/{session_id}/threads/{thread_id}/messages` → `ThreadMessagesResponse`

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/messages",
    operation_id="list_messages",
)
async def list_messages(
    session_id: str,
    thread_id: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> ThreadMessagesResponse:
```

### `GET /sessions/{session_id}/threads/{thread_id}/messages/{message_id}` → `SessionHistoryItem`

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/messages/{message_id}",
    operation_id="get_message",
)
```

### `POST /sessions/{session_id}/threads/{thread_id}/messages` → SSE

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/messages",
    operation_id="send_message",
)
async def send_message(
    session_id: str,
    thread_id: str,
    payload: MessageRequest,
    request_id: str = Query(default_factory=uuid.uuid4().hex),
) -> StreamingResponse:
```

Returns SSE stream via `_message_sse()`. SSE events: `type: "message"`,
`type: "history_updated"`, `type: "agent_configured"`,
`type: "queue_updated"`, `type: "end"`.

```python
class MessageRequest(WireModel):
    content: str = ""
    request_id: str = ""
    delivery: Literal["queue", "steer"] = "steer"
    images: list[ImageInput] = Field(default_factory=list)
    attachments: list[AttachmentInput] = Field(default_factory=list)
```

### `POST /sessions/{session_id}/threads/{thread_id}/events` → SSE

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/events",
    operation_id="stream_events",
)
async def stream_events(
    session_id: str,
    thread_id: str,
    after: int | None = None,
) -> StreamingResponse:
```

### `POST /sessions/{session_id}/threads/{thread_id}/interrupt` → `InterruptResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/interrupt",
    operation_id="interrupt",
)
```

### `POST /sessions/{session_id}/threads/{thread_id}/clear-history` → `HistoryMutationResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/clear-history",
    operation_id="clear_history",
)
```

### `POST /sessions/{session_id}/threads/{thread_id}/undo-history` → `HistoryMutationResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/undo-history",
    operation_id="undo_history",
)
async def undo_history(
    session_id: str,
    thread_id: str,
    payload: UndoRequest,
) -> HistoryMutationResponse:
```

### `POST /sessions/{session_id}/threads/{thread_id}/regenerate` → SSE

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/regenerate",
    operation_id="regenerate_message",
)
async def regenerate_message(
    session_id: str,
    thread_id: str,
    payload: RegenerateRequest,
) -> StreamingResponse:
```

### `GET /sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id}` → artifact

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id}",
    operation_id="get_artifact",
)
```

### `GET /sessions/{session_id}/threads/{thread_id}/pending-inputs` → `PendingInputListResponse`

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/pending-inputs",
    operation_id="list_pending_inputs",
)
```

### `POST /sessions/{session_id}/threads/{thread_id}/pending-inputs` → SSE

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/pending-inputs",
    operation_id="update_pending_input",
)
```

### `POST /sessions/{session_id}/threads/{thread_id}/interactions/permissions` → `InteractionResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/interactions/permissions",
    operation_id="respond_permission",
)
async def respond_permission(
    session_id: str,
    thread_id: str,
    payload: PermissionResponseRequest,
) -> InteractionResponse:
```

### `POST /sessions/{session_id}/threads/{thread_id}/interactions/user-input` → `InteractionResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/interactions/user-input",
    operation_id="respond_user_input",
)
```

### `POST /sessions/{session_id}/threads/{thread_id}/close` → `CloseResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/close",
    operation_id="close_thread",
)
```

## SSE format (`_format_sse`)

```python
def _format_sse(
    event: dict[str, Any],
    *,
    seq: int,
    session_id: str,
    thread_id: str,
    request_id: str,
) -> bytes:
```

Yields `data: {...}\n\n` with SSE fields: `event`, `id` (seq),
`session_id`, `thread_id`, `request_id`.

## Exception handlers

```python
exception_handlers=(
    (SessionNotFound, _session_not_found),    # → 404
    (ThreadNotActive, _thread_not_active),    # → 409
)
```

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`),
  `server_options`, `workspace_events`.
- Depended on by: HTTP clients, TUI, web interface.
- Pairs with: `process-sessions` (`SessionsPort`), `interactions`
  (interaction responses), `permission-request` (permission responses).

## Common pitfalls

- **SSE stream doesn't handle `OperationError`**: if the session
  closes mid-stream, the client gets a broken pipe. Use the
  `SessionEventCursorExpired` exception for expired cursors.
- **`history_limit` is 1–500**: `OpenSessionRequest.history_limit`
  and `UndoRequest.history_limit` both have `ge=1, le=500`. Values
  outside this range fail schema validation (400).
- **`MessageRequest` requires content**: `_require_content` validator
  raises if `content` is empty AND no images/attachments are provided.
- **`PendingInputUpdateRequest.action="edit"` requires non-empty
  content**: `_validate_edit` raises if `content.strip()` is empty.
