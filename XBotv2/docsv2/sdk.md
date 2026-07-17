# SDK Contract

XBot exposes an OpenAPI-described HTTP API at `/openapi.json`. Protocol v3 is
the source contract for generated third-party clients; the TUI uses the same
routes and DTOs and has no private control path.

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

Agent, provider, Tool, command, task, and history discovery are read-only API
operations. Tool schemas are exposed for inspection, but Tool execution remains
owned by the Agent runtime so permissions, sandboxing, Hooks, caching, and
persistence cannot be bypassed by an SDK client.
