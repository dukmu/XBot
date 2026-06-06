# Protocol And Client Events

## Transport

The production TUI protocol is HTTP + SSE.

The FastAPI app is created by `xbotv2.protocol.http_server.create_app()`. The
transport client is `xbotv2.tui.transport_http.HttpTransport`. `TerminalSession`
is the TUI-facing facade over the transport.

The legacy JSONL stdio server module remains in source as a non-primary path,
but Stage 2 TUI/server flows are HTTP-only.

## Session API

### `GET /health`

Returns server health, protocol version, uptime, session count, and server
workspace default.

### `POST /hello`

Request:

```json
{"session_id": "optional", "thread_id": "agent"}
```

Response includes `server_name`, `protocol_version`, `session_id`, and
`thread_id`.

### `POST /sessions`

Request:

```json
{
  "session_id": null,
  "thread_id": "agent",
  "workspace_root": "/repo/project",
  "mode": "new"
}
```

Modes:

- `new`: default. Creates a generated session id if omitted. Fails if explicit
  session state already exists.
- `resume`: requires an existing session id. Missing state returns 404.

Response:

```json
{
  "session_id": "20260606-143012-a8f2",
  "thread_id": "agent",
  "status": "ready",
  "agent_name": "XBotv2",
  "workspace_root": "/repo/project",
  "provider": "default"
}
```

One server can host sessions from different `workspace_root` values.

## Message Stream

### `POST /sessions/{session_id}/messages`

Sends one user message and returns `text/event-stream`.

Streamed event types include:

- `turn_started`, `turn_finished`, `turn_cancelled`
- `assistant_delta`, `assistant_message`
- `tool_calls_started`, `tool_call_delta`, `tool_result`
- `usage`
- `client_message`
- `permission_request`, `permission_denied`, `permission_response_recorded`
- `user_input_required`, `user_input_recorded`
- `error`
- terminal `end`

Only one active turn is allowed per session. A concurrent message returns an
`engine_busy` stream error.

## Live Interactions

### Permission And Sandbox Approval

When permission or sandbox policy requires approval, the engine emits
`permission_request` with:

```json
{
  "request_id": "permission:<tool_call_id>",
  "source": "permission_system|sandbox",
  "tool_call": {"name": "...", "args": {}, "id": "..."},
  "decision": "ask",
  "reason": "...",
  "resume_supported": false
}
```

The TUI responds through:

```http
POST /sessions/{session_id}/interactions/permission-response
```

Request:

```json
{"request_id": "permission:call_1", "decision": "allow", "scope": "once|session|always"}
```

Allow continues the current tool call. `scope="session"` and `scope="always"`
are forwarded to the engine so the approval can be persisted by the policy
layer; `scope="once"` is not persisted. Deny, timeout, disconnect, and non-live
execution fail closed.

### User Input

`ask_user` emits `user_input_required` and waits for:

```http
POST /sessions/{session_id}/interactions/user-input
```

Request:

```json
{"request_id": "user_input:call_1", "answer": "..."}
```

## Interrupt

`POST /sessions/{session_id}/interrupt` cancels the current turn task if one is
running. Idle interrupt is a successful no-op.

## Server Commands

The server owns runtime command metadata and execution.

Endpoints:

```http
GET /commands
GET /sessions/{session_id}/commands
POST /sessions/{session_id}/commands
```

Built-in server commands:

- `/status`
- `/provider status|list|use <name>`
- `/permission status|list|set <tool> <allow|deny|ask>|reset [tool]`
- `/sandbox status|list|set <key> <allow|readwrite|readonly|deny|ask>|reset [key]`

Policy command values are validated. `reset` updates both materialized state and
the active in-memory session policy, so command effects are immediate without
restarting the session.

Command results return:

```json
{
  "type": "command_result",
  "data": {
    "command": "status",
    "status": "ok",
    "message": "...",
    "data": {}
  }
}
```

Command results append `server_command_result` events for audit but do not enter
LLM message history.

## Error Shape

REST errors use stable JSON:

```json
{"code": "session_not_found", "message": "..."}
```

The HTTP transport parses this body before raising so TUI error text is stable.
