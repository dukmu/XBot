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
| GET | `/sessions/{session_id}/threads/{thread_id}/messages` | `list_messages` | cursor-paginated `HistoryPage[ConversationRecord]` |
| GET | `/sessions/{session_id}/threads/{thread_id}/trajectory` | `list_trajectory` | append-order `TrajectoryRead` |
| GET | `/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id:path}` | `get_artifact` | artifact bytes with media headers |
| POST | `/sessions/{session_id}/threads/{thread_id}/messages` | `send_message` | `202 Accepted` command submission |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/clear` | `clear_thread_history` | `HistoryMutation` |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/undo` | `undo_thread_history` | `HistoryMutation` |
| POST | `/sessions/{session_id}/threads/{thread_id}/history/regenerate` | `regenerate_message` | `202 Accepted` command submission |
| GET | `/sessions/{session_id}/threads/{thread_id}/queue` | `list_pending_inputs` | `PendingInputListResponse` |
| PATCH | `/sessions/{session_id}/threads/{thread_id}/queue/{message_id}` | `update_pending_input` | updated `PendingInputListResponse` |
| GET | `/sessions/{session_id}/threads/{thread_id}/events` | `stream_events` | replay/live SSE stream |

### History items and windowed reads

History reads and completed live-message events use the same discriminated
`ConversationRecord` projection (`human_input`, `runtime_notice`, `assistant`,
`tool`, or `compaction_summary`). Each record's required `id` is the canonical
conversation-message identity, so clients can merge live and paged records
without inventing a second identity. Trajectory positions remain a separate
identity for append-log entries and are not substituted for message IDs.

A client that does not want the whole conversation asks for a bounded window:

- `POST /sessions` (`open_session`) and `POST .../threads` (`open_thread`) accept
  `history_limit` (1-500). The response's `history` is a `HistoryPage` with the
  newest that many records and `older_cursor` naming the page before them; the
  cursor is `null` when the window already holds the beginning.
- `GET .../messages?limit=N` reads the newest `N` records, and
  `GET .../messages?limit=N&cursor=C` reads the page before `C`. The response's
  `older_cursor` is the cursor for the page before it, or `null` at the beginning.
  Appending records leaves a cursor valid; a rewrite (`/history/undo`,
  `/history/clear`, compaction) invalidates it, and the read then answers
  `invalid_cursor` rather than a page from a different history.
- History clear/undo responses return `HistoryMutation(removed_turns, history,
  stats)`. Its `history` is the same `HistoryPage` used by attach and
  `/messages`; clear also accepts the optional `history_limit` query parameter.
- `GET .../trajectory` returns `TrajectoryRead(page=HistoryPage[TrajectoryEntry],
  newest_position=...)` and also accepts `before=<position>` as an absolute
  anchor, which is what a client uses once it has released the front of its own
  window.

`MessageRequest` supports `content`, `request_id`, `delivery` (`queue` or
`steer`), images, and attachments. SSE envelopes carry sequence, session,
thread, typed event scope, kind, and payload. A `turn` scope carries the
independent `turn_id`; it is not the message's `request_id` or an interaction
id. The event stream is replayable from an opaque `after` cursor; an expired
cursor is an explicit conflict, not silent truncation.

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
  "sequence": 17,
  "scope": {"kind": "turn", "turn_id": "<turn>"},
  "kind": "assistant_completed | tool_completed | usage_updated | turn_ended | ...",
  "payload": {}
}
```

The `payload` object is validated by the producer-owned event schema in
`agentloop/protocol.py`, `session/protocol.py`, or the owning plugin's
`protocol.py`. Clients use `sequence` for ordering and surface an unsupported
kind instead of silently skipping it.

## Interactions

| Method | Path | Operation | Result |
|---|---|---|---|
| POST | `/sessions/{session_id}/threads/{thread_id}/interactions/permission-response` | `respond_permission` | `InteractionResponse` |
| POST | `/sessions/{session_id}/threads/{thread_id}/interactions/user-input` | `respond_user_input` | `InteractionResponse` |

Permission response bodies are owned by `permissions`; user-input bodies are
owned by `interactions`. A response ID is opaque and is not parsed by clients.

## Commands and Tools

The command plane exposes server command discovery and execution. The catalogue
shape is stable, while entries vary by active thread and loaded plugins. Client
commands may also appear in the catalogue, but they are local affordances and
are not executable through these HTTP routes. In the Textual TUI, `/help` is a
local command which lists local commands and the current server catalogue, and
`/help <command>` shows the selected entry's usage, parameters, and examples;
see [Clients and runtime behavior](clients.md). Third-party HTTP clients must
not assume TUI-local handlers exist.

| Field | Meaning |
|---|---|
| `name` / `slash` | the command and its `/` form |
| `kind` | who runs it: `client` (local affordance), `server` (this resource), `prompt` (submit the line as a message) |
| `description` / `usage` / `examples` / `parameters` | what the user reads |
| `effects` | what running it can touch (`history`, `thread`, `agents`, `jobs`, `commands`, `sessions`, `policy`), declared *before* it runs |
| `exclusive` | whether it must run while nothing else is |

`POST` takes **one line, exactly as typed**: `{"raw": "/model use m2"}`. The
server resolves the name from its own catalogue and hands the rest to the
command unchanged; a line the command cannot parse comes back as an error
*result* (`status: "error"`, with the reason in `message`), not as a transport
failure. There is no `kind` in the request: the catalogue already says who runs
the line, and repeating it in every client would be a second implementation of
the same rule. The route resolves against the server catalogue and dispatches
server commands; prompt entries are submitted through the message endpoint by a
client, and client entries never execute through this route. The returned
`effects` are the handler-reported changes for a successful (`status: "ok"`)
execution; error responses omit them. Do not infer effects from the command's
name or declaration alone.

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
| POST | `/sessions/{session_id}/threads/{thread_id}/jobs/stop` | `stop_all_jobs` | `JobStopResponse` |

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
