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
| POST | `/sessions/{session_id}/threads/{thread_id}/messages` | `send_message` | `202 Accepted` command submission |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/clear` | `clear_thread_history` | `HistoryMutationResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/undo` | `undo_thread_history` | `HistoryMutationResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/regenerate` | `regenerate_message` | `202 Accepted` command submission |
| GET | `/sessions/{session_id}/threads/{thread_id}/queue` | `list_pending_inputs` | `PendingInputListResponse` |
| PATCH | `/sessions/{session_id}/threads/{thread_id}/queue/{message_id}` | `update_pending_input` | updated `PendingInputListResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}/events` | `stream_events` | replay/live SSE stream |

`MessageRequest` supports `content`, `request_id`, `delivery` (`queue` or
`steer`), images, and attachments. SSE envelopes carry sequence, session,
thread, request, event type, and typed data. The event stream is replayable
from an opaque `after` cursor; an expired cursor is an explicit conflict, not
silent truncation.

Message and regeneration POSTs are command submissions and always return
`202 Accepted`, regardless of `Accept`. Subscribe to `GET .../events` for all
turn, tool, background-task, and regeneration events; it is the sole SSE
transport and supports replay through the `after` cursor.

Each connection owns only its cursor over the session's authoritative event
log. Closing the connection releases that cursor; it does not cancel the
session turn or any background job.

An attached session SSE consumer receives every live frame in sequence while
its cursor remains inside the retained replay window. The live notification is
only a bounded wakeup; a slow consumer is never silently truncated or
detached. If it falls behind the replay window, the server returns an
explicit cursor conflict. Clients should keep network reading independent
from expensive rendering and recover from a cursor conflict with the session
snapshot's returned `event_cursor`.

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

The command plane is **discovery plus execution of the slash vocabulary**, and it
is a normal, typed part of the API surface. What a client may rely on is the
resource: the catalogue's *shape* and the request's shape. What it may not rely
on is the *content*: which commands exist is decided per session and thread by
the plugins that are loaded there (the session plugin registers the built-ins,
capability plugins register theirs through `ctx.commands`), so the list changes
with the plugin tree and with the active agent. Clients therefore read it, they
never hardcode it.

| Field | Meaning |
|---|---|
| `name` / `slash` | the command and its `/` form |
| `kind` | who runs it: `server` (this resource), `prompt` (submit the line as a message: it is a prompt template) |
| `description` / `usage` / `examples` / `parameters` | what the user reads |
| `effects` | what running it can touch (`history`, `thread`, `agents`, `jobs`, `commands`, `sessions`), declared *before* it runs |
| `exclusive` | whether it must run while nothing else is |

`POST` takes **one line, exactly as typed**: `{"raw": "/model use m2"}`. The
server resolves the name from its own catalogue and hands the rest to the
command unchanged; a line the command cannot parse comes back as an error
*result* (`status: "error"`, with the reason in `message`), not as a transport
failure. There is no `kind` in the request: the catalogue already says who runs
the line, and repeating it in every client would be a second implementation of
the same rule.

One client procedure, the same in every client (TUI, Web, and any third party):

1. keep your own local commands (pure UI affordances) — they win over the
   server's catalogue;
2. `GET …/commands` on attach, and again after any switch that can change the
   set (session, thread, agent, provider);
3. a line that names a local command runs locally; a `prompt` command is sent to
   the message endpoint; anything else is `POST`ed here as one line;
4. show `message`; use `effects` to refresh what can have changed.

| Method | Path | Operation | Result |
|---|---|---|---|
| GET | `/sessions/{session_id}/threads/{thread_id}/commands` | `list_commands` | `CommandListResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/commands` | `run_command` | `CommandResponse` |
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
| GET | `/sessions/{session_id}/threads/{thread_id}/jobs` | `list_jobs` | `JobListResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/jobs/{job_id}/stop` | `stop_job` | `JobStopResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/jobs/stop` | `stop_all_tasks` | `JobStopResponse` |

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
