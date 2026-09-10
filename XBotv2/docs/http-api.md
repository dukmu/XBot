# HTTP and SSE API

This page is the endpoint index. The detailed request/response schemas,
operation ownership, SSE payloads, and router contribution examples are in the
skill's [HTTP route references](../.agents/skills/xbot-plugin-development/references/plugins/README.md)
and [XBot API guide](../.agents/skills/xbot-plugin-development/references/xbot-api.md).

The server is FastAPI with typed Pydantic wire models. The route owner is the
plugin's `protocol.py`; the root `plugin.py` contributes the router when the
server carrier and required services exist. `GET /hello` is not used: the
protocol handshake is `POST /hello`.

## Core

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/health` | `health` | `HealthResponse` |
| POST | `/hello` | `hello` | `HelloResponse`; rejects unsupported protocol versions with 426 |

## Sessions and threads

| Method | Path | Operation | Result |
|---|---|---|---|
| POST | `/sessions` | `open_session` | `OpenSessionResponse` |
| GET | `/sessions` | `list_sessions` | `SessionListResponse` and catalog cursor |
| GET | `/sessions/{session_id}` | `get_session` | `SessionSummary` |
| PATCH | `/sessions/{session_id}` | `rename_session` | `SessionSummary` |
| POST | `/sessions/{session_id}/fork` | `fork_session` | `ForkResponse` |
| DELETE | `/sessions/{session_id}` | `delete_session` | `DeleteSessionResponse` |
| GET | `/sessions/{session_id}/threads` | `list_threads` | `ThreadListResponse` |
| POST | `/sessions/{session_id}/threads` | `open_thread` | `OpenSessionResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}` | `get_thread` | `ThreadSummary` |
| POST | `/sessions/{session_id}/close` | `close_session` | `CloseResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/close` | `close_thread` | `CloseResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/interrupt` | `interrupt_thread` | `InterruptResponse` |

## Messages, history, queue, and stream

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/sessions/{session_id}/threads/{thread_id}/messages` | `list_messages` | cursor-paginated `ThreadMessagesResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}/trajectory` | `list_trajectory` | append-order `ThreadTrajectoryResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id:path}` | `get_artifact` | artifact bytes with media headers |
| POST | `/sessions/{session_id}/threads/{thread_id}/messages` | `send_message` | SSE turn stream |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/clear` | `clear_thread_history` | `HistoryMutationResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/undo` | `undo_thread_history` | `HistoryMutationResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/regenerate` | `regenerate_message` | SSE turn stream |
| GET | `/sessions/{session_id}/threads/{thread_id}/queue` | `list_pending_inputs` | `PendingInputListResponse` |
| PATCH | `/sessions/{session_id}/threads/{thread_id}/queue/{message_id}` | `update_pending_input` | updated `PendingInputListResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}/events` | `stream_events` | replay/live SSE stream |

`MessageRequest` supports `content`, `request_id`, `delivery` (`queue` or
`steer`), images, and attachments. SSE envelopes carry sequence, session,
thread, request, event type, and typed data. The event stream is replayable
from an opaque `after` cursor; an expired cursor is an explicit conflict, not
silent truncation.

Queued and steering inputs use the same replayable stream as conversation
output. `input_accepted`, `input_claimed`, and `input_consumed` carry
`{"message_ids": [...]}` (and `target` for acceptance), allowing a client to
show delivery progress after reconnect. `input_consumed` is emitted from the
typed Inbox commit boundary; it is not an Engine-private marker.

The trajectory endpoint is the cold-replay source for clients that present a
unified activity stream. Its opaque cursor remains valid when new records are
appended. Each item is a discriminated `message`, `surface_replace`, or `event`
record with a monotonic `position`; clients fold replacement records instead
of treating them as additional chat messages.

Request/response schemas, cursor constraints, error codes, and router examples
are intentionally maintained in the skill rather than duplicated here.

```json
{
  "protocol_version": "xbotv2.v3",
  "session_id": "<session>",
  "thread_id": "<thread>",
  "request_id": "<request>",
  "sequence": 17,
  "type": "assistant_message | tool_result | usage | turn_finished | ...",
  "data": {}
}
```

The `data` object is validated by the producer-owned event schema in
`agentloop/protocol.py`, `session/protocol.py`, or the owning plugin's
`protocol.py`; clients should preserve unknown event types and use `sequence`
for ordering.

## Interactions

| Method | Path | Operation | Result |
|---|---|---|---|
| POST | `/sessions/{session_id}/threads/{thread_id}/interactions/permission-response` | `respond_permission` | `InteractionResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/interactions/user-input` | `respond_user_input` | `InteractionResponse` |

Permission response bodies are owned by `permissions`; user-input bodies are
owned by `interactions`. A response ID is opaque and is not parsed by clients.

## Commands and Tools

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/sessions/{session_id}/threads/{thread_id}/commands` | `list_commands` | `CommandListResponse` (hidden from OpenAPI schema) |
| POST | `/sessions/{session_id}/threads/{thread_id}/commands` | `run_command` | `CommandResponse` (hidden from OpenAPI schema) |
| GET | `/sessions/{session_id}/threads/{thread_id}/tools` | `list_tools` | `ToolListResponse` |

## Agent and model selection

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/sessions/{session_id}/threads/{thread_id}/agents` | `list_agents` | `AgentListResponse` |
| PUT | `/sessions/{session_id}/threads/{thread_id}/agent` | `select_agent` | `AgentSelectionResponse` |
| GET | `/providers` | `list_providers` | `ProviderCatalog` |
| PUT | `/sessions/{session_id}/threads/{thread_id}/provider` | `select_provider` | `ProviderSelectionResponse` |
| PUT | `/sessions/{session_id}/threads/{thread_id}/effort` | `select_effort` | `EffortSelectionResponse` |

## Policy, Todo, and jobs

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/sessions/{session_id}/policy` | `get_session_policy` | `SessionPolicyResponse` |
| PATCH | `/sessions/{session_id}/policy` | `update_session_policy` | `SessionPolicyResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}/todos` | `get_todos` | Todo response |
| GET | `/sessions/{session_id}/threads/{thread_id}/tasks` | `list_tasks` | `TaskListResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop` | `stop_task` | `TaskStopResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/tasks/stop` | `stop_all_tasks` | `TaskStopResponse` |

## Plugin configuration

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/sessions/{session_id}/threads/{thread_id}/plugin-config?scope=global\|workspace\|session` | `list_plugin_config` | `PluginConfigCatalog` |
| PATCH | `/sessions/{session_id}/threads/{thread_id}/plugin-config/{plugin_id}?scope=global\|workspace\|session` | `update_plugin_config` | updated `PluginConfigCatalog` |

The catalog is built from the loaded plugin tree. Each entry carries the
plugin-declared JSON Schema, the raw configuration at the selected layer, the
effective merged value, and an opaque content revision. The Web client uses
the same generic editor for every plugin; it does not contain plugin-name
specific controls. A PATCH must send the revision returned by GET, so a stale
tab receives `plugin_config_conflict` rather than overwriting another edit.

`global` writes `<data-dir>/config/plugins.yaml`, `workspace` writes the
active workspace `.xbot/plugins.yaml`, and `session` writes the active
session `sessions/<session_id>/config.yaml`. These are overlays of the same
plugin declaration grammar. Resolution is always:

```text
xcore.yaml → data/config/plugins.yaml → workspace/.xbot/plugins.yaml
           → sessions/<session_id>/config.yaml → in-memory launch overrides
```

The update is validated against the declared XCore schema and written
atomically. Global/workspace changes affect later application starts; session
changes affect that session's next runtime. It is not a live reload mechanism.
Plugins without a producer `Config` schema are listed but explicitly
read-only. Provider secrets and plugin-specific constraints not expressible by
the declared schema remain server-owned limitations rather than being guessed
by the client. External plugins follow the normal workspace `plugins.yaml`
tree and Python import environment; the catalog does not introduce a second
plugin-directory discovery mechanism.

## Workspaces and directories

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/directories` | `list_workspace_directories` | `DirectoryListing` |
| GET | `/workspaces` | `list_workspaces` | `WorkspaceListResponse` |
| GET | `/workspaces/events` | `stream_workspace_events` | catalog SSE stream |
| POST | `/workspaces` | `create_workspace` | `WorkspaceCreateResponse` |
| PATCH | `/workspaces/{workspace_id}` | `rename_workspace` | `WorkspaceResponse` |
| DELETE | `/workspaces/{workspace_id}` | `delete_workspace` | `WorkspaceDeleteResponse` |
| POST | `/workspaces/{workspace_id}/order` | `reorder_workspace` | `WorkspaceOrderResponse` |
| POST | `/workspaces/{workspace_id}/sessions/{session_id}/order` | `reorder_workspace_session` | `WorkspaceResponse` |
| PUT | `/sessions/{session_id}/archive` | `archive_session` | `ArchivedSessionsResponse` |
| DELETE | `/sessions/{session_id}/archive` | `unarchive_session` | `ArchivedSessionsResponse` |

## Errors and compatibility

All route errors use the shared `ErrorResponse` envelope with a stable code,
message, details, and retryability. Wire models reject unknown fields where
declared. `POST /hello` negotiates `PROTOCOL_VERSION`; clients must not infer
feature support from a route's incidental HTTP status.
