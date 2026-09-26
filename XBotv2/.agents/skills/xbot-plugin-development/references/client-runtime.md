# Client/runtime contracts

This reference describes the client-facing behavior that plugin authors need to
preserve. Wire schemas belong to their owning protocol package; consult
[XBot API](xbot-api.md), [session routes](plugins/server-routes-session.md),
[interactions](plugins/interactions.md), and [permissions](plugins/permissions.md)
for complete declaration details.

## Client ownership

The Agent runtime and session manager own execution, thread state, and
persistence. HTTP/SSE, Textual, Web, and ACP are transport/client boundaries;
client code does not call the Engine directly. The terminal UI is a Textual
client mounted through the `tui` client plugin and the generic client launch
contract. The current implementation lives under `XBotv2/tui/`, with transport,
state/controller, timeline, and Textual view layers.

Subagent threads are represented by ordinary thread summaries with
`kind="subagent"`, `parent_thread_id`, and runtime status. Textual supports
opening and viewing these threads in a read-only client mode. That presentation
mode must not be described as a server-side authorization guarantee: API write
behavior is determined by the session protocol and policy.

## Dynamic commands and help

Plugins register human-facing commands through `ctx.commands`. `Command.kind`
is `server`, `prompt`, or `client`; only server and client commands have
handlers. `GET /sessions/{session_id}/threads/{thread_id}/commands` returns the
current thread catalogue, including kind, usage, examples, parameters, effects,
and exclusivity. Its contents depend on the loaded plugins and active thread.

`POST .../commands` accepts `{"raw": "/name arguments"}`. The server resolves
the command from its catalogue. It executes a server entry and returns a typed
command result; prompt entries must be submitted through the message endpoint,
and local client entries do not execute through this route. Do not add a client
`kind` field to the request or dispatch slash commands as synthetic Tool calls.

The Textual client merges its local presentation commands with the server
catalogue. It gives local commands precedence on name collisions, refreshes the
catalogue as runtime selection or thread/session context changes, and resolves
entries for completion and command execution. `/help` lists the merged entries;
`/help <command>` renders that entry's published description, usage,
parameters, and examples. These are Textual-local handlers, not server commands
that a third-party client can invoke. Other clients can use the catalogue to
build their own discovery/help UI.

## Streaming

Session event frames are validated typed events with sequence, session/thread
identity, and a scope. Turn scope carries `turn_id`; it is separate from the
input `request_id` and any interaction ID. The protocol currently distinguishes
`assistant_text_delta` and `assistant_reasoning_delta`. Tool argument updates
may use `tool_call_delta`; `tool_calls_started` supplies complete calls before
execution, and `tool_completed` supplies the typed execution result. Completed
messages and history records have their own canonical IDs. Keep live ordering,
conversation identity, and append-log positions separate.

The replay cursor is opaque and valid only inside the retained event window.
Expired cursors are explicit conflicts. A client recovers from the session or
thread snapshot and its returned cursor. Disconnecting the event stream does
not cancel the turn. Provider-specific wire chunks, usage decoding, and retry
semantics stay inside provider implementations.

## Typed interactions

Permission and user-input requests are separate models, not stringly typed
client notices. `PermissionRequest` has `interaction_id`, `source`, `reason`,
`resume_supported`, and a discriminated `subject`: `ToolPermission` carries a
concrete `ToolCall`, while `NamedPermission` carries a Tool name and parameter
values. A permission response is `{request_id, decision, scope}`, with decision
`allow|deny` and scope `once|session`.

`UserInputRequest` has `interaction_id`, `source`, `tool_call_id`, `question`,
optional typed `options`, optional `timeout_seconds`, and
`resume_supported`. Its response is `{request_id, answer}`. `ask_user` requires
at least two options. The owning permission and interactions services register
waiters before publishing requests; attach/resume snapshots expose pending
interactions separately from event replay so the client can rebuild a live
dialog. An interaction ID is not the outer turn ID or message request ID.

Permission grants authorize Tool calls under permission policy; they do not
change sandbox capabilities. User-input answers provide data to the waiting
Tool; they do not authorize it. Do not collapse these flows into one generic
approval model.

## History, resume, status, and usage

An attach may request `history_limit` (1–500); message history and append-order
trajectory are distinct read models with cursor contracts. Compaction appends a
surface replacement and leaves earlier trajectory records durable. Resume
hydrates persisted thread state but does not replay prior permission decisions
or user-input answers. Pending live interactions are exposed separately when
they remain answerable.

Thread summaries report `turn_status` (`idle` or `running`), `usage`, and
`status_slots`. Status slots are plugin-published string values, not a fixed set
of global fields. `UsageSnapshot` keeps cumulative `TokenCounters` separate
from the latest request observation; context may be `ProviderMeasured` or
`MeasurementUnavailable`. Do not describe unavailable context as an exact
count or infer one from cumulative input tokens.
