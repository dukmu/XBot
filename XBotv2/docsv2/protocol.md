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

`shutdown` closes the active engine session before emitting `shutdown_ok`.
That path runs `ON_SESSION_CLOSE`, appends `session_closed`, saves messages,
and materializes `state.yaml` with `status: closed`.

## Interaction Semantics

- `send_message` emits `client_message` and does not stop the current turn.
- `ask_user` emits `user_input_required`, appends an `interrupted` state event,
  and stops the current turn. Resume is not implemented yet, so the payload
  includes `resume_supported: false`.
- A later `turn_started` reactivates the materialized session status after an
  interruption or error; `turn_finished` does not clear an interruption raised
  during that same turn.
- Permission and sandbox ask decisions emit `permission_request` and fail
  closed. Denials emit `permission_denied`.
- Before client-directed events are persisted and streamed, core runs the
  generic `ON_CLIENT_EVENT` hook so plugins can audit, meter, or mirror
  interaction traffic.

## Client Coverage

- `TerminalSession` streams every server frame until `turn_finished` or
  `error`.
- `CursesTuiClient` consumes protocol events only; it does not import runtime,
  core, LangChain, or LangGraph modules.
- `TuiState` renders assistant messages, tool calls/results, errors, client
  notices, approval requests, denials, and user-input requests.
- TUI state treats approval requests, permission denials, user-input requests,
  and errors as terminal notice states for that turn. A following
  `turn_finished` frame updates the turn number but does not overwrite the
  visible waiting/denied/error status.
