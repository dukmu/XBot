# Clients and runtime behavior

XBotv2 has one session/thread runtime and typed transport contracts. The
Textual terminal UI and Web UI are clients of the server API; they do not own
Agent execution or persistence. ACP is another carrier. The detailed wire
schemas and routes are indexed in [HTTP API](http-api.md), and plugin authors
can use the version-matched [client/runtime skill reference](../.agents/skills/xbot-plugin-development/references/client-runtime.md).

## Textual TUI

The terminal client is implemented with Textual in `XBotv2/tui/`. It is mounted
by the `tui` client plugin through the generic client launch contract. Transport
and controller code operate on typed client events and a bounded transcript
window; Textual widgets live under `tui/view/`. The TUI asks the server for
history pages and can page older history. Its window and retained-history limits
are client launch settings; do not infer server-side history deletion from
client retention.

Run it with `xbot tui` (or invoke `xbot` without a subcommand). Unless
`--server URL` is supplied, the CLI starts a local API server for the client
and cleans it up on exit. `--session ID` attaches to an existing session;
`--history-window N` controls fetched entries per attach/page and
`--history-retention N` controls client-held entries before older released
pages are dropped. Defaults are 50 and 2,000. The `tui` plugin's
`TextualTuiConfig` separately controls `render_interval` (default `0.1`
seconds) and `transcript_limit` (default `100` entries per render window).
Other supported CLI modes are `xbot serve`, `xbot web`, `xbot once`, and
`xbot acp`; these have distinct carrier behavior, so a TUI-local command such
as `/help` is not implied to exist in another mode.

## Commands

The server publishes a per-thread command catalogue through `GET
/sessions/{session_id}/threads/{thread_id}/commands`. Plugins register server
and prompt commands with `ctx.commands`. The TUI merges that catalogue with its
local presentation commands, with local names taking precedence, and refreshes
the server catalogue when changing thread/session or runtime selection.

In the TUI, `/help` lists the currently available merged catalogue and `/help
<command>` shows one command's advertised usage, parameters, and examples. This
help handler is local to the Textual client; other clients should use the
catalogue endpoint and implement their own help presentation. Server commands
are posted as one raw line to the command route. Prompt commands go through the
message endpoint. `client` entries are local only and cannot be invoked through
the server command route. The catalogue describes actual registrations rather
than a fixed list of all possible commands.

## Streaming and conversation records

The session event stream carries validated, typed event kinds with a monotonic
sequence and session/thread scope; turn-scoped events also carry a `turn_id`.
Text and reasoning arrive as separate `assistant_text_delta` and
`assistant_reasoning_delta` events. Tool-call argument deltas may arrive while
the call is being assembled; `tool_calls_started` carries the complete call
before execution, and `tool_completed` carries the execution result. Completed
assistant/tool messages are also represented in history, so clients use the
event sequence for live ordering and canonical message IDs for history merging.
Provider-specific streaming formats remain inside provider adapters.

The stream can be replayed from its opaque cursor while it remains in the
retained window. An expired cursor is an explicit conflict; reconnect clients
should reopen a session/thread snapshot and use its returned cursor. A client
disconnect does not cancel a running turn. See the HTTP API page for the
trajectory log and detailed replay behavior.

## Permission and user-input interactions

Permission approval and user-input requests are distinct typed interactions.
Permission requests carry an opaque `interaction_id`, source, reason, and a
discriminated subject (`tool` with a concrete Tool call, or `named` with a Tool
name and parameters). A permission response supplies `request_id`, `decision`
(`allow` or `deny`), and approval `scope` (`once` or `session`). User-input
requests carry their own interaction ID, question, source, optional choices,
timeout, and Tool call ID; their response supplies `request_id` and an answer.
These IDs are not the outer turn ID or input request ID.

An attach/resume snapshot includes unanswered `pending_interactions`, allowing
clients to reconstruct dialogs after reconnect. Live requests are registered
before they are exposed to the client. Permission grants do not change sandbox
policy, and user-input requests do not authorize Tools. See [Security](security.md)
and the skill's [interaction](../.agents/skills/xbot-plugin-development/references/plugins/interactions.md)
and [permission](../.agents/skills/xbot-plugin-development/references/plugins/permissions.md)
references.

## Session, thread, resume, and compaction

A session contains threads. Thread summaries expose identity, kind, parent,
turn status, runtime selection, usage, and status slots. The Textual TUI can
switch between threads and inspect subagent threads in read-only mode; that UI
mode is a client behavior, not a claim that every transport rejects writes to a
subagent thread. Background tasks and subagent threads have separate lifecycle
and read models.

Opening a persisted session/thread in `resume` mode hydrates its durable history,
plugin state, and runtime selection according to the owning stores. It does not
replay an old permission decision or user answer. Pending live interactions are
included separately when they remain answerable. Compaction appends a semantic
surface replacement to the trajectory; it does not delete the earlier durable
trajectory or transcript records. Details are in [Persistence](persistence.md)
and the skill's [session trace](../.agents/skills/xbot-plugin-development/references/session-trace.md).

## Status, usage, and context

Thread state exposes `turn_status` (`idle` or `running`), `status_slots`, and a
typed `UsageSnapshot`. The TUI derives its status label from connection state,
the server's turn status, pending interactions, compaction, interrupt state,
and running background jobs. Status slots are plugin-published display values,
not a global fixed schema.

Usage snapshots separate cumulative token counters from the latest request's
context observation. Context observation may be provider-measured or explicitly
unavailable; a missing measurement should not be presented as an exact context
count. These values describe runtime/provider observations and do not promise a
provider-independent exact tokenizer measurement.
