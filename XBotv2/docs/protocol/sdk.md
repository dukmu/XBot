# SDK Contract

XBot exposes an OpenAPI-described HTTP API at `/openapi.json`. Protocol v3 is
the source contract for generated third-party clients. Human slash parsing is
not part of this contract; SDK clients call typed resource operations.

## Resource Model

- A session is the persistent container for policy, artifacts, and threads.
- A thread owns one conversation, usage totals, Agent selection, interactions,
  and live tasks.
- The main thread has no parent. Subagent threads record `parent_thread_id`,
  inherit the parent's permission intersection, and cannot create subagents.
- `open(mode="resume")` attaches to an active runtime or reconstructs an
  inactive persisted thread. It never replaces an active runtime.
- Closing a client transport or event subscription is `detach`; it does not
  close a thread. `interrupt` cancels a turn. Explicit `close` releases live
  runtimes but never deletes persisted resources.

Every thread operation is rooted at:

```text
/sessions/{session_id}/threads/{thread_id}
```

The exact public path set and unique OpenAPI `operationId` values are contract
tested. Adding, removing, or renaming a route requires a protocol version
decision and corresponding tests.

## Python Client

`client.XBotClient` is the first-party asynchronous client. It returns
the same Pydantic models used by the wire contract and raises
`XBotClientError` with `status_code`, `code`, `details`, and `retryable` fields.

```python
from client import XBotClient

async with XBotClient("http://127.0.0.1:4096") as client:
    session = await client.open_session(workspace_root=".")
    async for event in client.send_message(
        session.session_id,
        session.thread_id,
        "Inspect the workspace",
    ):
        if event.type == "assistant_message":
            print(event.data["content"])
```

The client also accepts `uds_path` for the local Unix-socket server. It has no
slash command or direct Tool execution methods. The TUI HTTP transport delegates
public operations and SSE decoding to this client, adding only tracing, dict
adaptation, and the plugin-command compatibility route.

Session and thread DTOs expose `model_mode` only when the selected provider
explicitly configures a reasoning effort or thinking mode. They also expose
plugin `status_slots`; these values are display metadata, not Agent context or
an alternate plugin mutation API.

## Mutations

Machine clients use explicit operations rather than constructing slash text:

```text
POST /sessions/{session_id}/fork
DELETE /sessions/{session_id}
GET   /sessions/{session_id}/policy
PATCH /sessions/{session_id}/policy
POST /sessions/{session_id}/threads/{thread_id}/history/clear
POST /sessions/{session_id}/threads/{thread_id}/history/undo
POST /sessions/{session_id}/threads/{thread_id}/history/regenerate
GET  /sessions/{session_id}/threads/{thread_id}/queue
PATCH /sessions/{session_id}/threads/{thread_id}/queue/{message_id}
GET  /sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id}
GET  /sessions/{session_id}/threads/{thread_id}/todos
PUT  /sessions/{session_id}/threads/{thread_id}/agent
PUT  /sessions/{session_id}/threads/{thread_id}/provider
POST /sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop
POST /sessions/{session_id}/threads/{thread_id}/tasks/stop
```

Thread-scoped history and configuration mutations require an active, idle
thread. Session policy can also be updated while all threads are inactive, but
rejects an active turn or background task. Busy mutations return
`409 thread_busy` with `retryable=true`. Missing Agents, providers, and tasks
return typed `404` errors. Stopping an already terminal task is an idempotent
success. Fork also rejects pending or running background tasks.

Session policy patches update exact top-level Tool decisions and sparse sandbox
keys. They preserve parameter-specific permission rules and sandbox resource
approvals. The server persists the patch to the session `config.yaml` and updates every
active thread without replacing parent/child permission intersections.
The policy response keeps session-local overrides in `permissions` and
`sandbox`; `effective_permissions` and `effective_sandbox` report the merged
global, session, and workspace policy. Agent definitions and inherited
subagent restrictions may narrow those base permissions. The TUI `/permission
status` and `/sandbox status` commands display the effective values. `/sandbox set
enabled false` disables isolation between turns, but Tool permissions continue
to apply.

Interactive clients use a compatibility endpoint to discover and execute
registered human slash commands. It is intentionally omitted from OpenAPI and
generated SDKs. UI-local lifecycle commands may call typed resources directly;
server-owned commands use the compatibility route.

## Streaming

`POST .../messages` returns the events for one submitted turn as SSE.
`XBotClient.send_message(..., images=[...])` accepts image objects with base64
`data` and an `image/*` `media_type`. Provider discovery reports
`input_modalities`; clients should only offer image input when `image` is
present.
`XBotClient.send_message(..., attachments=[...])` uploads arbitrary files.
Each item contains base64 `data`, `media_type`, and `name`. History returns a
session-relative artifact reference, not the original bytes.
Use `XBotClient.read_artifact(...)` to retrieve referenced bytes and
`XBotClient.regenerate_message(...)` to replace the latest human turn without
duplicating it. `list_messages(..., limit=..., cursor=...)` reads newest-first
pages while returning each page's messages in conversation order.
During a running turn, `send_message(..., delivery="queue")` keeps the input
out of the transcript until the next turn claims it; `delivery="steer"` folds
it into the current turn at the next step boundary. `list_pending_inputs()` and
`update_pending_input(..., action="edit" | "remove" | "steer")` operate on the
same durable Agent inbox projection.
`GET .../events` is the single-consumer stream for server-initiated turns and
task notifications. Both streams carry validated `ServerEvent` envelopes.
Compaction remains a foreground, session-busy operation. Its lifecycle is
reported through `compaction_started`, `compaction_completed`,
and `compaction_failed`; completion carries metrics and updated session usage.

Generated clients may need a small transport adapter for SSE because OpenAPI
describes the endpoint but not incremental event iteration. Clients should use
the outer `request_id` to correlate a turn and the interaction request id inside
`data` when replying to permission or user-input requests.

## Versioning

Clients must call `/hello` with their supported `protocol_version` before
opening resources. Backward-incompatible paths or payload changes increment the
wire version. Additive optional fields may remain within the current version.

The current server is loopback-only and has no remote authentication contract.
Remote SDK use requires a trusted tunnel until authentication is implemented.

## Boundaries

Agent, provider, Tool, task, history, session, and thread state are exposed as
typed resources. Tool schemas are available for inspection, but Tool execution
remains owned by the Agent runtime so permissions, sandboxing, Hooks, caching,
and persistence cannot be bypassed by an SDK client. The HTTP API never invokes
a Tool directly and never sends a slash command to the model.
