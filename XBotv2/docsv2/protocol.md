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
python -m xbotv2 tui                           # UDS (default)
python -m xbotv2 tui --server http://..        # attach to HTTP/SSE server
python -m xbotv2 serve                         # server-only on 127.0.0.1
```

Local Web mode also uses a generated UDS for its API subprocess by default,
but browsers never access that socket directly. A loopback Python Web server
serves the compiled HTML/JS and proxies same-origin `/api/*` requests to the
UDS after removing the `/api` prefix. Both normal JSON responses and SSE streams
retain the API status, content type, and body. The API subprocess and socket are
removed when Web mode exits.

```bash
python -m xbotv2 web                           # compiled Web + automatic UDS API
python -m xbotv2 web --server http://127.0.0.1:4096
```

`--server` and `--uds` are mutually exclusive. The second form proxies an
existing HTTP API instead of spawning one. The browser-facing server binds
only to `127.0.0.1` while authentication is unavailable.

## Session

A session is a persistent container with one main thread and zero or more
subagent threads. Live runtimes are addressed by `(session_id, thread_id)`;
opening or closing one subagent thread does not replace the main thread.
Thread status and history remain queryable after its runtime closes.

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
  and private provider metadata. Tool history retains structured `data`,
  `error`, and `artifacts` so resumed clients render the same Details content
  as the live event stream.
- `OpenSessionResponse.model`, `model_mode`, and `context_window` describe the
  active provider model, its explicitly configured reasoning/thinking mode, and
  the runtime context budget. An empty `model_mode` means the provider did not
  configure one; XBot does not invent a default mode. Provider selection is
  persisted in thread metadata and restored by `resume`.
- `status_slots` is a compact `dict[str, str]` supplied by loaded plugins for
  human status displays. It appears on open/thread responses and turn-finished
  events. The Goal plugin exposes its current state as the `goal` slot.
- `OpenSessionResponse.usage` restores cumulative session token totals and the
  latest provider-reported `context_tokens`. Live `usage` events are per-model-
  call deltas; clients add them to the restored totals.
  Core persists these totals independently in `state/usage.yaml`, so compact,
  clear, and undo do not erase token accounting.
- Protocol/configuration text is UTF-8. Clients do not attempt Latin-1 or
  CP1252 repair when text is already decoded.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/providers` | List provider names and non-secret capabilities |
| POST | `/hello` | Client handshake |
| POST | `/sessions` | Open session (new/resume) |
| GET | `/sessions` | List persisted sessions and runtime status |
| GET | `/sessions/{sid}` | Read one session summary |
| POST | `/sessions/{sid}/fork` | Copy persisted session state to a new id |
| GET | `/sessions/{sid}/policy` | Read session-local policy rules |
| PATCH | `/sessions/{sid}/policy` | Update session-local permission and sandbox rules |
| GET | `/sessions/{sid}/threads` | List main and subagent threads |
| POST | `/sessions/{sid}/threads` | Open a new or persisted subagent thread |
| GET | `/sessions/{sid}/threads/{tid}` | Read thread status and usage |
| GET | `/sessions/{sid}/threads/{tid}/agents` | List workspace-visible Agents |
| PUT | `/sessions/{sid}/threads/{tid}/agent` | Select the active Primary Agent |
| PUT | `/sessions/{sid}/threads/{tid}/provider` | Select and persist the provider |
| GET | `/sessions/{sid}/threads/{tid}/tools` | List model-visible Tool schemas |
| GET | `/sessions/{sid}/threads/{tid}/messages` | Read display-safe history |
| POST | `/sessions/{sid}/threads/{tid}/messages` | Send message, receive turn SSE |
| POST | `/sessions/{sid}/threads/{tid}/history/clear` | Clear conversation history |
| POST | `/sessions/{sid}/threads/{tid}/history/undo` | Undo complete user turns |
| GET | `/sessions/{sid}/threads/{tid}/events` | Receive server-initiated events |
| GET | `/sessions/{sid}/threads/{tid}/tasks` | List shell and subagent tasks |
| POST | `/sessions/{sid}/threads/{tid}/tasks/{task_id}/stop` | Stop one task idempotently |
| POST | `/sessions/{sid}/threads/{tid}/tasks/stop` | Stop all running tasks |
| POST | `/sessions/{sid}/threads/{tid}/interrupt` | Cancel running turn |
| POST | `/sessions/{sid}/threads/{tid}/interactions/permission-response` | Submit permission decision |
| POST | `/sessions/{sid}/threads/{tid}/interactions/user-input` | Submit user input answer |
| POST | `/sessions/{sid}/threads/{tid}/close` | Close one thread runtime |
| POST | `/sessions/{sid}/close` | Close all live runtimes in a session |

`close` never deletes persisted history, artifacts, policy, or plugin state.
Slash command transport is not part of the OpenAPI/SDK resource contract.

The session-open body may include `agent` to select a plugin-registered Primary
Agent for a new thread. Resume reads the Agent identity from thread metadata.

The typed history, session, Agent, provider, and task endpoints above are the
machine API. Human slash commands remain TUI adapters:

- `/undo [count]` removes complete user turns from the persisted tail; `count`
  defaults to one.
- `/clear` removes all message history while preserving the session id, policy,
  artifacts, and plugin state.
- `/fork` copies persisted state, artifacts, plugin state, and policy to a new
  session id. It rejects live turns, interactions, and background tasks rather
  than copying changing runtime state.
- `/agent list` discovers Primary Agents and `/agent use <name>` changes the
  active Agent for subsequent turns without replacing the thread or history.
- `/tasks [ps]` lists live background shell and subagent tasks. `/task stop <id>` and
  `/task stopall` control them without sending command text to the model.

The Goal plugin separately registers the human `/goal` command and the Agent
Tools `create_goal`, `get_goal`, and `update_goal`. The command endpoint invokes
the plugin's command handler directly; it never translates slash text into a
Tool call.

Typed history mutations return `HistoryMutationResponse.messages`, which is the
same display-safe state the next provider request will use.

## Runtime Mailbox

Each connected session owns an in-memory mailbox. `user_message` and `general`
are its only message kinds. User messages have priority, and FIFO order is
preserved within each kind. Idle human input bypasses the mailbox. A message
submitted behind an active turn or existing queue receives `message_queued`;
the server, rather than the TUI, controls delivery.

`general` messages carry source and metadata inside their payload. When the
session becomes idle, Core builds one explicitly labelled, non-persisted runtime
input from the payload and the owning plugin's current state. The provider sees
a transient user-role envelope because supported chat protocols need a final
input to trigger generation, but its content and internal metadata state that it
is not human input. It never enters conversation history as a user message.
These turns cover Goal continuation and runtime notifications. Their output
uses `GET /sessions/{sid}/threads/{tid}/events`; queued human turns keep their
originating message stream. The thread event stream remains open across separate `general`
turns and closes only when the client disconnects or the session ends.

The mailbox queue is not persistent state. Closing or losing the client
connection drops queued messages, and `resume` starts with an empty mailbox.
Once delivery starts, Core also appends a `mailbox_delivery` record to
`state/messages.jsonl`; replay retains it as audit evidence but does not turn it
back into a queued item or provider Message. The separate append-only
`logs/mailbox.jsonl` file remains lower-level queue diagnostic evidence.

XBot conversation history uses the provider-neutral roles `system`, `user`,
`assistant`, and `tool`. Only human input is persisted with `user`; transient
runtime input is source-labelled and excluded from history. Provider adapters
own wire conversion: OpenAI-compatible
requests receive one leading instruction message, while Anthropic receives the
same instruction text through its top-level `system` field and groups adjacent
Tool results into one user content block. OpenAI's `developer` role is a
provider capability, not a portable XBot history role.

## TUI Command Compatibility

The TUI owns built-in human commands and executes them through typed HTTP
resources. A non-OpenAPI compatibility route discovers only plugin commands
and prompt expansions with a `kind` field:

```json
[
  {"name": "goal", "kind": "server", "description": "Manage the session goal"},
  {"name": "find-skills", "kind": "prompt", "description": "Find skills"}
]
```

Kinds: `client` (local TUI only), `server`, and `prompt`.

Each server command registry entry contains human-facing discovery metadata and
an async handler that receives the unparsed argument text. Human syntax belongs
to that command's domain; the protocol does not derive a CLI from JSON Schema.
Server commands execute deterministically outside model history. Plugin
commands own plugin-specific business state. Built-ins such as `/undo`,
`/provider`, and `/tasks` never enter this registry: the TUI parser calls the
corresponding typed API through `XBotClient`.

A `prompt` entry has metadata but no command handler. The client submits its
original slash text through the message endpoint, where the owning plugin
expands it before the accepted user message enters history. Agent Tools keep
structured JSON-schema inputs and use the Tool runtime. Ordinary Tools and MCP
Tools are not slash commands and are not returned by command discovery.

## Stream Events

Every SSE `data:` payload is a `ServerEvent` envelope:

```json
{
  "protocol_version": "xbotv2.v3",
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
| `message_queued` | `{message_id, position}` |
| `assistant_message_delta` | `{content}` or `{reasoning}` |
| `assistant_message` | `{content, tool_calls}` |
| `tool_call_delta` | `{tool_calls: [{tool_call_id, id, name, args_delta, args, index, replaces_tool_call_id?}]}` |
| `tool_calls_started` | `{tool_calls: [{id, name, args, type}]}` |
| `tool_result` | `{tool_call_id, name, content, status, data?, error?, artifacts?}` |
| `task_updated` | `{task_id, kind, command, cwd, status, created_at, started_at, finished_at, output, error, agent?, thread_id?, usage?}` |
| `client_message` | `{message, level, source, tool_call_id}` |
| `permission_denied` | `{request_id, reason, tool_call, decision}` |
| `permission_request` | `{request_id, source, reason, tool_call, decision, resume_supported}` |
| `permission_response_recorded` | `{request_id, status, decision, scope, answer, pending_interactions}` |
| `user_input_required` | `{request_id, source, tool_call_id, question, options, timeout_seconds, resume_supported}` |
| `user_input_recorded` | `{request_id, status, decision, scope, answer, pending_interactions}` |
| `usage` | `{input_tokens, output_tokens, total_tokens, requests, context_tokens}` |
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
User-input responses accept an arbitrary JSON-compatible `answer`. `ask_user`
requires at least two `{label, description}` choices and returns the selected
label; other interaction sources may request free-form input. The timeout, when
present, is positive.

The Agent-facing `request_permission` Tool emits the same
`permission_request` event with a `permission` object containing an exact Tool
name and full-match parameter regular expressions. It does not fabricate or
execute a future ToolCall, and it does not define another Tool dispatch path.
An allow-once rule is consumed by the next matching call.

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

- HTTP: `invalid_request`, `interaction_no_longer_pending`,
  `parent_thread_not_active`, `session_busy`, `session_exists`,
  `session_not_found`, `session_open_failed`, `thread_not_active`,
  `unsupported_protocol`;
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
