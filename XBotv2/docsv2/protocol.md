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
and materializes `state.yaml` with `status: closed`.

## Interaction Semantics

- `send_message` emits `client_message` and does not stop the current turn.
- `ask_user` emits `user_input_required`, appends an `interrupted` state event,
  and stops the current turn. Answers can be recorded with `user.input`, but
  turn resume is not implemented yet, so the payload includes
  `resume_supported: false`. The event carries a stable
  `request_id` (`user_input:<tool_call_id>`), `source`, and `tool_call_id` so a
  later `user.input` command can correlate the answer.
- A later `turn_started` reactivates the materialized session status after an
  interruption or error; `turn_finished` does not clear an interruption raised
  during that same turn.
- Permission and sandbox ask decisions emit `permission_request` and fail
  closed. Request events carry `request_id` (`permission:<tool_call_id>`) and
  `source`; denials emit `permission_denied`.
- `user.input` records a `user_input_response` event and returns
  `user_input_recorded`. `permission.response` records a `permission_response`
  event and returns `permission_response_recorded`. Both commands clear the
  matching `pending_interactions` entry but do not resume the interrupted turn
  yet.
- `state.yaml` materializes unresolved interaction requests as
  `pending_interactions`, rebuilt from the append-only event log.
- Before client-directed events are persisted and streamed, core runs the
  generic `ON_CLIENT_EVENT` hook so plugins can audit, meter, or mirror
  interaction traffic.

## Client Coverage

- `TerminalSession` streams every server frame until `turn_finished` or
  `error`, and exposes helper methods for `user.input` and
  `permission.response`.
- `CursesTuiClient` consumes protocol events only; it does not import runtime,
  core, LangChain, or LangGraph modules.
- `TuiState` renders assistant messages, tool calls/results, errors, client
  notices, approval requests, denials, user-input requests, and recorded
  response acknowledgements.
- TUI state treats approval requests, permission denials, user-input requests,
  and errors as terminal notice states for that turn. A following
  `turn_finished` frame updates the turn number but does not overwrite the
  visible waiting/denied/error status.
