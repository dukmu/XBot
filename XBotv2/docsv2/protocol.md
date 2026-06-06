# Protocol And Client Events

XBotv2 uses JSONL `ProtocolFrame` envelopes for all server/client traffic.
Runtime events are translated to frames by `ProtocolEncoder` or passed through
with their event type and payload.

## Event Surface

Core Phase 1-3 events covered by subprocess tests:

- `turn_started`, `turn_finished`
- `assistant_message`
- `tool_calls_started`, `tool_result`
- `client_message`
- `permission_request`, `permission_denied`
- `user_input_required`
- `user_input_recorded`, `permission_response_recorded`
- `error`
- `session_ready`, `hello_ok`, `shutdown_ok`

Every streamed frame for a `user.message` request preserves the incoming
`request_id` in the envelope.

Runtime identifiers from protocol frames are validated before bootstrap creates
session paths. Path-like or empty identifiers fail closed as bounded
`session_open_failed` errors.

`shutdown` closes the active engine session before emitting `shutdown_ok`.
That path runs `ON_SESSION_CLOSE`, appends `session_closed`, saves messages,
and materializes `state.yaml` with `status: closed` and no pending
interactions.

Server, TUI, terminal, and once modes accept `--no-plugins` for pure-core
smoke tests. Default runtime mode still scans the built-in plugin root.

## Interaction Semantics

- `send_message` emits `client_message` and does not stop the current turn.
- `ask_user` is a live human-in-the-loop tool. During an active `user.message`
  stream it emits `user_input_required`, waits for a matching `user.input`
  frame on the same connection, and returns the answer as the tool result so
  the ReAct loop can continue. The event carries a stable `request_id`
  (`user_input:<tool_call_id>`), `source`, `tool_call_id`, and
  `resume_supported: false` because pending questions are not durable across
  client disconnects or server restarts.
- If `ask_user` times out, core records `user_input_cancelled`, returns a
  no-reply tool result, and lets the agent continue. If the client disconnects
  while waiting, core records `user_input_cancelled` plus `turn_cancelled`,
  leaves the materialized status `interrupted`, and does not continue the
  current turn.
- A later `turn_started` reactivates the materialized session status after an
  interruption or error; `turn_finished` does not clear an interruption raised
  during that same turn.
- Permission and sandbox ask decisions emit `permission_request`, wait for a
  matching live `permission.response`, and continue the current tool call when
  the client allows it. Deny, timeout, disconnect, or non-live runtimes fail
  closed. Request events carry `request_id` (`permission:<tool_call_id>`) and
  `source`; denials emit `permission_denied`.
- A live `user.input` records a `user_input_response` event and returns
  `user_input_recorded` before the tool result is emitted. A live
  `permission.response` records a `permission_response` event and returns
  `permission_response_recorded` before the approved tool result is emitted.
  Response events include a snapshot of the original pending request payload
  for audit.
- `state.yaml` materializes unresolved interaction requests as
  `pending_interactions`, rebuilt from the append-only event log.
- Before client-directed events are persisted and streamed, core runs the
  generic `ON_CLIENT_EVENT` hook so plugins can audit, meter, or mirror
  interaction traffic.

## Client Coverage

- `TerminalSession` streams every server frame until `turn_finished` or
  `error`; `send_message_with_input()` can answer live `ask_user` requests
  through an input provider. Helper methods remain for standalone
  `user.input` and `permission.response` commands.
- Live interaction events are yielded exactly once. The client sees
  `permission_request` / `user_input_required` before any provider callback is
  awaited; if no provider is installed, the event is still yielded once and the
  session does not auto-answer.
- `TextualTuiClient` is the default `--mode tui` frontend. It consumes protocol
  events only through `TerminalSession`, uses the same `user.input` and
  `permission.response` command surface for live interactions, and does not
  import runtime, core, LangChain, or LangGraph modules.
- Textual rendering treats missing child widgets during streaming as a normal
  intermediate state. A pending `tool_call_delta` may create a tool entry with
  only metadata; later detail/body updates must not crash or disconnect the SSE
  stream.
- `CursesTuiClient` remains available as the legacy `--mode curses` fallback
  and follows the same protocol-only boundary.
- `TuiState` renders assistant messages, tool calls/results, errors, client
  notices, approval requests, denials, user-input requests, and recorded
  response acknowledgements.
- TUI state treats approval requests, permission denials, user-input requests,
  and errors as terminal notice states for that turn. During a live
  `ask_user`, the next typed line is sent as the answer instead of starting a
  new user turn.

## HTTP Error Shape

All REST endpoints return stable JSON errors with `{"code": str,
"message": str}`. The TUI HTTP transport parses that body before raising so
the visible error is `code: message` rather than a generic HTTP status line.
Server bootstrap failures, including provider/API-key misconfiguration, use
`session_open_failed` with the original diagnostic message.
