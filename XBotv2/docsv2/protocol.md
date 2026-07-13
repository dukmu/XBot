# XBotv2 Protocol

## Transport

XBotv2 uses HTTP JSON endpoints plus SSE streams for the active C/S path.
Local TUI mode can spawn the same HTTP server behind a Unix domain socket.

- **Unix Domain Socket** (default for local TUI): auto-generated at
  `/tmp/xbotv2-{pid}.sock`. Server subprocess spawned and bound to it.
  No TCP port needed. Socket cleaned up on exit.

- **HTTP/SSE** (TCP): `--server URL` connects the TUI to an existing server.
  Server mode binds `--bind`:`--port` (default `127.0.0.1:4096`). Non-loopback
  bind is rejected until authentication exists.

TUI transport is selectable at startup:
```bash
python -m xbotv2 --mode tui                    # UDS (default)
python -m xbotv2 --mode tui --server http://.. # attach to HTTP/SSE server
python -m xbotv2 --mode server                 # server-only on 127.0.0.1
```

## Session

### Modes

- `new`: create session, generate session_id if not provided
- `resume`: reconnect to existing session state on disk
- `new` with an existing explicit id returns HTTP 409; `resume` with a missing
  id returns HTTP 404. Resume always replaces any same-process runtime and
  rebuilds the engine from persisted history.
- The CLI treats an explicit TUI `--session` as `resume`; omitting it creates a
  new generated session. Programmatic clients continue to send the mode
  explicitly.
- `OpenSessionResponse.history` contains display-safe user, assistant, and tool
  messages as typed `SessionHistoryItem` values. It excludes system messages
  and private provider metadata.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/hello` | Client handshake |
| POST | `/sessions` | Open session (new/resume) |
| POST | `/sessions/{id}/messages` | Send message, receive SSE stream |
| POST | `/sessions/{id}/interrupt` | Cancel running turn |
| GET | `/sessions/{id}/commands` | List available commands (includes skills/tools) |
| POST | `/sessions/{id}/commands` | Execute server/skill/tool command |
| POST | `/sessions/{id}/interactions/permission-response` | Submit permission decision |
| POST | `/sessions/{id}/interactions/user-input` | Submit user input answer |
| POST | `/sessions/{id}/shutdown` | Close session |

Session history commands use the same command endpoint:

- `/undo [count]` removes complete user turns from the persisted tail; `count`
  defaults to one.
- `/clear` removes all message history while preserving the session id, policy,
  artifacts, and plugin state.
- `/fork` copies persisted state, artifacts, plugin state, and policy to a new
  session id without copying a live turn or interaction.

The Goal plugin also exposes a deterministic `/goal` server command. `/goal`
inspects, plain text creates, and the `complete`, `block`, `resume`, and `clear`
actions transition the same state machine exposed to the model as the `goal`
tool. Goal mutations are rejected while a turn is active.

`CommandResult.history` is normally `null`. `clear` and `undo` set it to the
resulting display history so clients can rebuild their transcript from the same
state the next provider request will use. Command-specific values such as
`removed_turns` remain in `CommandResult.data`.

## Command System

`GET /sessions/{id}/commands` returns unified command list with `kind` field:

```json
[
  {"name": "status", "kind": "server", "description": "Server status"},
  {"name": "shell", "kind": "tool", "description": "Execute shell"},
  {"name": "find-skills", "kind": "skill", "description": "Find skills"},
  {"name": "search", "kind": "mcp", "description": "MCP search"}
]
```

Kinds: `client` (local TUI only), `server`, `skill`, `tool`, `mcp`.

Each server command is one registry entry containing both discovery metadata
and its async handler. The dispatcher normalizes the name and performs one
lookup; extending the registry does not require a second parsing branch.

Session command discovery includes server commands and registered tools. Tool,
skill, and MCP command execution is being aligned with the tool system contract;
until then, slash command discovery should not be treated as a separate tool
invocation protocol.

## Stream Events

Every SSE `data:` payload is a `ServerEvent` envelope:

```json
{
  "protocol_version": "xbotv2.v1",
  "session_id": "session-1",
  "thread_id": "agent",
  "request_id": "client-request-1",
  "sequence": 1,
  "type": "assistant_message",
  "data": {"content": "hello"}
}
```

The SSE `event:` field is the same value as envelope `type`. The SSE `id:`
field is the same value as envelope `sequence`.

`MessageRequest.request_id` is the correlation key for one submitted message
and its turn. The server generates `req-<uuid>` when the client sends an empty
value. That final id is passed to `Engine.run_turn`, exposed on every
turn-scoped `HookContext`, and copied to every SSE envelope emitted for the
request, including errors and `end`.

Interaction ids are a separate namespace and lifecycle. For example, a
`permission_request` event has the turn correlation id in the outer envelope
and the pending permission id in `data.request_id`. Clients respond using the
inner interaction id while continuing to correlate the stream using the outer
turn id. Interaction ids are opaque: clients associate acknowledgements with
the request event they observed and must not parse prefixes or derive tool-call
ids from them.

Both sides use `xbotv2.protocol.sse` for the wire format. The server encodes a
validated `ServerEvent`; the client incrementally decodes SSE messages and then
validates their JSON payload as `ServerEvent`. UI code only receives validated
event dictionaries and does not parse SSE lines itself. `TerminalSession`
consumes the final `end` sentinel, so UI reducers receive domain events only.

| Event | Data |
|---|---|
| `turn_started` | `{turn}` |
| `turn_finished` | `{turn}` |
| `turn_cancelled` | `{turn, reason}` |
| `assistant_message_delta` | `{content}` or `{reasoning}` |
| `assistant_message` | `{content, tool_calls}` |
| `tool_call_delta` | `{tool_calls: [{tool_call_id, id, name, args_delta, args, index, replaces_tool_call_id?}]}` |
| `tool_calls_started` | `{tool_calls: [{id, name, args, type}]}` |
| `tool_result` | `{tool_call_id, name, content, status, data?, error?, artifacts?}` |
| `client_message` | `{message, level, source, tool_call_id}` |
| `permission_denied` | `{request_id, reason, tool_call, decision}` |
| `permission_request` | `{request_id, source, reason, tool_call, decision, resume_supported}` |
| `permission_response_recorded` | `{request_id, status, decision, scope, answer, pending_interactions}` |
| `user_input_required` | `{request_id, source, tool_call_id, question, options, timeout_seconds, resume_supported}` |
| `user_input_recorded` | `{request_id, status, decision, scope, answer, pending_interactions}` |
| `usage` | `{input_tokens, output_tokens, total_tokens, requests}` |
| `error` | `{code, message, details?, retryable?, stage?}` |
| `end` | `{status}` |

After `turn_started`, the stream emits exactly one turn terminal event:
`turn_finished` for normal or failed completion, or `turn_cancelled` for an
interrupt. A failed turn emits its diagnostic `error` immediately before
`turn_finished`; clients retain the error status while clearing active-turn
state. `end` is a transport sentinel indicating that the SSE response closed
cleanly. Its `status` does not describe the semantic outcome of the turn.

The current event type inventory lives in
`xbotv2.protocol.models.KNOWN_SERVER_EVENT_TYPES`. Golden SSE fixtures live
under `XBotv2/tests/fixtures/sse/`.

## Agent-Initiated Interaction

`permission_request` and `user_input_required` are blocking server-to-client
requests. Their shared lifecycle is:

1. The engine registers the interaction `request_id`.
2. The server publishes the request on the active message SSE stream.
3. The client submits a response to the matching interaction endpoint.
4. The server publishes `permission_response_recorded` or
   `user_input_recorded` on the original stream.
5. Tool execution resumes with the decision or answer.

Registration happens before publication, so a client may respond immediately
after receiving the request. Repeated or stale responses return HTTP 410.
The client must continue consuming the SSE stream while local input is pending;
it submits the response independently through the interaction endpoint. A turn
terminal event invalidates any unanswered request.
Permission responses accept `allow` or `deny` with `once` or `session` scope.
User-input responses accept an arbitrary JSON-compatible `answer`; the
request may include suggested string `options` and a timeout.

Because `ask_user` is a registered tool, permission policy runs first. Under an
`ask` policy, one turn therefore carries two ordered interactions:
`permission_request`, then `user_input_required`. The client must resolve each
request by its own id and continue consuming the same SSE stream.

The protocol DTO inventory for this family is:

- `PermissionResponseRequest` and `UserInputResponseRequest` for HTTP bodies;
- `PermissionRequestData` and `UserInputRequiredData` for blocking SSE events;
- `InteractionRecordedData` for the two response-recorded events.

Stable non-interaction event DTOs currently include `ToolResultData`,
`UsageData`, `ErrorEventData`, `TurnData`, `TurnCancelledData`,
`AssistantMessageData`, `AssistantMessageDeltaData`, `ClientMessageData`,
`ToolCallData`, `ToolCallDeltaData`, and `ToolCallsStartedData`. HTTP failures
use `ErrorResponse`; all HTTP exception handlers serialize through that model
and always return `code`, `message`, `details`, and `retryable`.

`ServerEvent` validates every current event payload at construction and decode
boundaries. `TYPED_SERVER_EVENT_TYPES` must cover the complete
`KNOWN_SERVER_EVENT_TYPES` inventory. A malformed payload decodes as an
`sse_decode_error` event instead of terminating the client stream.

Error codes remain strings rather than an enum. The current server-owned
inventory is:

- HTTP: `invalid_request`, `interaction_no_longer_pending`, `session_exists`,
  `session_not_found`, `session_open_failed`, `unsupported_protocol`;
- SSE: `engine_busy`, `engine_error`, `hook_short_circuit_rejected`,
  `sse_decode_error`, `stream_failed`, `turn_failed`,
  `user_message_rejected`.

Unexpected engine exceptions use `engine_error` and carry the Python exception
class in `details.exception_type`; class names are not wire error codes. Hooks
may emit extension-defined string codes, so this inventory is a maintained
behavior list rather than a closed enum.

Interaction recovery after an SSE disconnect is not supported. Request events
therefore carry `resume_supported: false`; disconnect cancels the live wait,
stops the affected turn, and destroys its runtime. The engine appends error tool
results for calls left unanswered by that interruption, so a new runtime can
resume the valid persisted history. Old interaction request IDs remain invalid.

The HTTP turn bridge owns and explicitly closes the Engine async stream on
normal completion, interrupt, and disconnect.

## Provider Events (internal)

`_model_response` event carries an aggregated `ModelResponse` with
content, tool_calls, usage_metadata, additional_kwargs (including
reasoning_content if present).
