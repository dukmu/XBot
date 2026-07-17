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
- `close` releases live runtimes. It never deletes persisted resources.

Every thread operation is rooted at:

```text
/sessions/{session_id}/threads/{thread_id}
```

The exact public path set and unique OpenAPI `operationId` values are contract
tested. Adding, removing, or renaming a route requires a protocol version
decision and corresponding tests.

## Python Client

`xbotv2.client.XBotClient` is the first-party asynchronous client. It returns
the same Pydantic models used by the wire contract and raises
`XBotClientError` with `status_code`, `code`, `details`, and `retryable` fields.

```python
from xbotv2.client import XBotClient

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
GET   /sessions/{session_id}/policy
PATCH /sessions/{session_id}/policy
POST /sessions/{session_id}/threads/{thread_id}/history/clear
POST /sessions/{session_id}/threads/{thread_id}/history/undo
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
approvals. The server persists the patch to `policy.yaml` and reloads every
active thread without replacing parent/child permission intersections.

The TUI currently has a compatibility endpoint for discovery and execution of
plugin-defined human slash commands. It is intentionally omitted from OpenAPI
and generated SDKs. Built-in TUI commands parse human syntax in the client and
call typed SDK methods. Only plugin-owned commands use the compatibility route.

## Streaming

`POST .../messages` returns the events for one submitted turn as SSE.
`GET .../events` is the single-consumer stream for server-initiated turns and
task notifications. Both streams carry validated `ServerEvent` envelopes.

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
