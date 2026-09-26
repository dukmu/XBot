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
| `GET` | `/sessions/{session_id}/threads/{thread_id}/messages` | `list_messages` | `HistoryPage[ConversationRecord]` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/trajectory` | `list_trajectory` | `TrajectoryRead` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id:path}` | `get_artifact` | binary artifact |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/history/clear` | `clear_thread_history` | `HistoryMutation` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/history/undo` | `undo_thread_history` | `HistoryMutation` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/history/regenerate` | `regenerate_message` | `202 Accepted`; events arrive on `/events` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/messages` | `send_message` | `202 Accepted`; events arrive on `/events` |
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

## History, queue, and mutation response schemas

```python
HistoryPage[ConversationRecord](items=..., older_cursor=...)

TrajectoryRead(
    page=HistoryPage[TrajectoryEntry](items=..., older_cursor=...),
    newest_position=...,
)

class PendingInputListResponse(WireModel):
    session_id: str
    thread_id: str
    items: list[PendingInputData]

@dataclass(frozen=True, slots=True)
class HistoryMutation:
    removed_turns: int
    history: HistoryPage[ConversationRecord]
    stats: SessionStats
```

`GET .../messages` returns `HistoryPage[ConversationRecord]` directly, with
the newest page in `items` and `older_cursor` for the preceding page.
`GET .../trajectory` returns the persistence-owned trajectory read model;
trajectory positions and conversation-record `id` values are separate
identities.

`OpenSessionResponse` wraps an `OpenedThread` projection. It contains resolved
thread metadata, a projected `HistoryPage[ConversationRecord]`, pending inputs,
and unanswered interactions; it is not the same object as internal
`OpenedThread` before protocol projection.

`pending_interactions` replays the payload of every live interaction the
session is still waiting on (`permission_request`, `user_input_required`).
A client that reloads or reconnects must rebuild its dialog from this field:
the request otherwise exists only in the event stream and is lost once the
bounded replay window evicts it.

## SSE

There is exactly **one** session SSE endpoint:
`GET /sessions/{session_id}/threads/{thread_id}/events`
(`operation_id="stream_events"`). It streams validated typed `ServerEvent`
envelopes and accepts an optional non-negative `after` sequence cursor. A stale
cursor returns `409` with `session_event_cursor_expired` and `oldest_sequence`
details; a malformed one returns `400`
`invalid_session_event_cursor`. Do not describe a separate request-scoped
"message stream" — message delivery arrives on this same cursor-based stream.

Session-owned event kinds observed on the stream are:

```text
agent_configured
history_updated
message
queue_updated
```

The stream also carries Agent-loop events (assistant deltas/messages, tool calls
and executions, turn lifecycle, usage, input rejection, and errors). It is
cursor-based and thread-scoped; `after` is a sequence number, not a message id.

`assistant_message.data.id` carries the durable record `id` and is the key
clients use to correlate one projected message with its record, so it must be
unique per response. A retried or finalizing iteration is a different response
and must not reuse the previous iteration's id. The wire field is `id`; there is
no `xbot_message_id`.

## Errors and boundaries

`SessionNotFound` maps to 404 and `ThreadNotActive` maps to 409. Invalid
request models are rejected by Pydantic at the HTTP boundary. HTTP code must
call `SessionsPort`; it must not instantiate `SessionRuntime`, reconstruct
paths, or write JSONL files.

Related references: [process-sessions.md](process-sessions.md),
[session.md](session.md), [interactions.md](interactions.md),
[persistence.md](persistence.md).
