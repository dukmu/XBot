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
- `error`
- `session_ready`, `hello_ok`, `shutdown_ok`

Every streamed frame for a `user.message` request preserves the incoming
`request_id` in the envelope.

## Interaction Semantics

- `send_message` emits `client_message` and does not stop the current turn.
- `ask_user` emits `user_input_required`, appends an `interrupted` state event,
  and stops the current turn. Resume is not implemented yet, so the payload
  includes `resume_supported: false`.
- Permission and sandbox ask decisions emit `permission_request` and fail
  closed. Denials emit `permission_denied`.

## Client Coverage

- `TerminalSession` streams every server frame until `turn_finished` or
  `error`.
- `CursesTuiClient` consumes protocol events only; it does not import runtime,
  core, LangChain, or LangGraph modules.
- `TuiState` renders assistant messages, tool calls/results, errors, client
  notices, approval requests, denials, and user-input requests.
