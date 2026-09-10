# `server-routes-session`

HTTP/SSE transport for process sessions and threads. The root `session`
plugin contributes this router after `server`, `sessions`,
`server_options`, and `workspace_events` are available. The router does not
open persistence files directly; all state is accessed through `SessionsPort`.

Source: `XBotv2/session/plugin.py`, `XBotv2/session/protocol.py`.

## Router contract

```python
def build_session_router(
    *,
    sessions: SessionsPort,
    options: ServerOptions,
    workspace_events: WorkspaceEventCursor,
) -> APIRouter: ...
```

The exact request and response models are declared in
`XBotv2/session/protocol.py`; the domain ports and shared models are in
`XBotv2/session/contracts.py`.

## Routes

| Method | Path | Operation ID | Response |
|---|---|---|---|
| `POST` | `/sessions` | `open_session` | `OpenSessionResponse` |
| `GET` | `/sessions` | `list_sessions` | `SessionListResponse` |
| `GET` | `/sessions/{session_id}` | `get_session` | `SessionSummary` |
| `PATCH` | `/sessions/{session_id}` | `rename_session` | `SessionSummary` |
| `POST` | `/sessions/{session_id}/fork` | `fork_session` | `ForkResponse` |
| `DELETE` | `/sessions/{session_id}` | `delete_session` | `DeleteSessionResponse` |
| `POST` | `/sessions/{session_id}/close` | `close_session` | `CloseResponse` |
| `GET` | `/sessions/{session_id}/threads` | `list_threads` | `ThreadListResponse` |
| `POST` | `/sessions/{session_id}/threads` | `open_thread` | `OpenSessionResponse` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}` | `get_thread` | `ThreadSummary` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/messages` | `list_messages` | `ThreadMessagesResponse` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/trajectory` | `list_trajectory` | `ThreadTrajectoryResponse` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id:path}` | `get_artifact` | binary artifact |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/history/clear` | `clear_thread_history` | `HistoryMutationResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/history/undo` | `undo_thread_history` | `HistoryMutationResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/history/regenerate` | `regenerate_message` | SSE |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/messages` | `send_message` | SSE |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/queue` | `list_pending_inputs` | `PendingInputListResponse` |
| `PATCH` | `/sessions/{session_id}/threads/{thread_id}/queue/{message_id}` | `update_pending_input` | `PendingInputListResponse` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/events` | `stream_events` | SSE |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/interactions/permission-response` | `respond_permission` | `InteractionResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/interactions/user-input` | `respond_user_input` | `InteractionResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/close` | `close_thread` | `CloseResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/interrupt` | `interrupt_thread` | `InterruptResponse` |

`/events` is a `GET` stream. Queue updates are `PATCH` requests addressed by
the queued `message_id`. Permission responses use the singular
`permission-response` path.

## Request schemas

```python
class OpenSessionRequest(WireModel):
    session_id: str | None = None
    thread_id: str = "agent"
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)

class OpenThreadRequest(WireModel):
    thread_id: str = Field(min_length=1)
    parent_thread_id: str = Field(default="agent", min_length=1)
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)

class MessageRequest(WireModel):
    content: str = ""
    request_id: str = ""
    delivery: Literal["queue", "steer"] = "steer"
    images: list[ImageInput] = []
    attachments: list[AttachmentInput] = []

class UndoRequest(WireModel):
    count: int = Field(default=1, ge=1)
    history_limit: int | None = Field(default=None, ge=1, le=500)

class RegenerateRequest(WireModel):
    request_id: str = ""

class PendingInputUpdateRequest(WireModel):
    action: Literal["edit", "remove", "steer"]
    content: str = ""
```

The actual Python models use `Field(default_factory=list)` for mutable list
defaults; the snippet uses JSON-schema-like shorthand only for readability.
`MessageRequest` rejects an empty text request unless an image or attachment
is supplied. Queue `edit` requires non-empty trimmed content.

## Response schemas

```python
class ThreadMessagesResponse(WireModel):
    session_id: str
    thread_id: str
    messages: list[SessionHistoryItem]
    next_cursor: str | None = None

class ThreadTrajectoryResponse(WireModel):
    session_id: str
    thread_id: str
    items: list[
        SessionTrajectoryMessage
        | SessionTrajectorySurfaceReplace
        | SessionTrajectoryEvent
    ]
    next_cursor: str | None = None

class PendingInputListResponse(WireModel):
    session_id: str
    thread_id: str
    items: list[PendingInputData]

class HistoryMutationResponse(WireModel):
    session_id: str
    thread_id: str
    removed_turns: int
    messages: list[SessionHistoryItem]
    session_stats: SessionStats
    history_cursor: str | None = None
```

`SessionTrajectoryMessage.message_id` is the stable user, assistant, or Tool
correlation key used when a durable page overlaps event-stream replay. It is
trajectory metadata and is intentionally not added to the legacy
`ThreadMessagesResponse` projection.

`OpenSessionResponse` and `SessionDescriptor` are Pydantic models. The
response contains the resolved runtime descriptor, a projected history page,
and pending inputs; it is not the same object as the internal `OpenedSession`.

## SSE

Both message and event streams use `text/event-stream` and the shared
`_sse_response`/`_format_sse` implementation. The session stream accepts an
optional non-negative `after` sequence cursor. A stale cursor returns `409`
with `session_event_cursor_expired` and `oldest_sequence` details.

Message stream session-owned event types are:

```text
agent_configured
history_updated
message
queue_updated
```

The general session event stream carries the validated `ClientEvent` stream,
including Agent-loop events (`assistant_message`, deltas, tool calls/results,
turn lifecycle, usage, input rejection, and errors). Do not treat the two
streams as interchangeable: the message stream is request-scoped, while the
event stream is cursor-based and thread-scoped.

## Errors and boundaries

`SessionNotFound` maps to 404 and `ThreadNotActive` maps to 409. Invalid
request models are rejected by Pydantic at the HTTP boundary. HTTP code must
call `SessionsPort`; it must not instantiate `SessionRuntime`, reconstruct
paths, or write JSONL files.

Related references: [process-sessions.md](process-sessions.md),
[session.md](session.md), [interactions.md](interactions.md),
[persistence.md](persistence.md).
