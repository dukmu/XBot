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

## Mutations

Machine clients use explicit operations rather than constructing slash text:

```text
POST /sessions/{session_id}/fork
POST /sessions/{session_id}/threads/{thread_id}/history/clear
POST /sessions/{session_id}/threads/{thread_id}/history/undo
PUT  /sessions/{session_id}/threads/{thread_id}/agent
PUT  /sessions/{session_id}/threads/{thread_id}/provider
POST /sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop
POST /sessions/{session_id}/threads/{thread_id}/tasks/stop
```

History and configuration mutations require an active, idle thread. A running
turn returns `409 thread_busy` with `retryable=true`. Missing Agents, providers,
and tasks return typed `404` errors. Stopping an already terminal task is an
idempotent success.

The TUI currently has a compatibility endpoint for discovery and execution of
plugin-defined human slash commands. It is intentionally omitted from OpenAPI
and generated SDKs. Commands parse human syntax and then call the same runtime
operations as the typed API; they are not an alternate machine API.

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
