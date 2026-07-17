# XBotv2 TUI Requirements

Status: Stage 2 runtime and TUI command model.

Last reviewed: 2026-07-13

## Goals

The XBotv2 TUI is a protocol client for the HTTP/SSE runtime. It borrows useful
OpenCode interaction patterns while preserving XBotv2 constraints:

- no runtime engine imports in TUI modules
- HTTP/SSE transport by default
- workspace-root sessions instead of internal session workspaces
- server-owned runtime commands
- live approval UI for permission, sandbox, and `ask_user` interactions

## Non-Goals

- Do not clone OpenCode's Bun/OpenTUI/Solid stack.
- Do not implement OpenCode's full command set.
- Do not reintroduce personalities or local TUI runtime config ownership.
- Do not persist server command results as LLM-visible messages.

## Runtime Connection

Startup sequence:

1. `GET /health`
2. `POST /hello`
3. `GET /commands`
4. `POST /sessions` with `workspace_root` and `mode`
5. Register local and server command completion metadata

`TerminalSession` owns the lifecycle from the TUI perspective. It wraps a
`Transport` implementation, usually `HttpTransport`.

## Session Semantics

- Default session mode is `new`.
- Omitted session id generates a new timestamp id.
- Explicit `mode="resume"` requires existing state and fails with 404 when
  missing.
- Supplying TUI `--session` selects resume mode. Omitting it selects new mode.
- Resume rebuilds the visible transcript from the typed display history in the
  open-session response before accepting another message.
- The TUI passes `workspace_root` to the server. The server may host sessions
  from multiple workspace roots in one process.
- The TUI displays runtime session/provider/workspace status from server command
  results and protocol events.

## Commands

Local commands:

- `/exit` (`/quit`, `/q`): quit the TUI
- `/clear-screen` (`/cls`): clear the visible transcript without changing the
  server session; server-owned `/clear` clears persisted session history
- `/help`: render all known local and server command labels
- `/thinking [on|off|toggle]`: control current and future reasoning blocks
- `/details [on|off|toggle]`: control current and future tool detail blocks

Server commands discovered from `GET /commands`:

- `/status`
- `/provider status|list|use <name>`
- `/permission status|list|set <tool> <allow|deny|ask>|reset [tool]`
- `/sandbox status|list|set <key> <allow|readwrite|readonly|deny|ask>|reset [key]`

Slash dispatch rules:

- local commands execute locally
- registered server commands call `POST /sessions/{sid}/commands`
- discovered Tool, Skill, and MCP commands are sent as normal user messages;
  the Agent invokes their registered tool when appropriate
- unknown slash commands render a local notice
- normal text is sent as a user message

Server command results render as transcript notices but do not enter LLM message
history.

Policy commands validate keys and values. `set` and `reset` both update the
session overrides and the live in-memory session policy.

## Composer

The composer is a bottom multiline `ComposerTextArea`.

Required behavior:

- `Enter` submits text.
- multiline editing remains supported by Textual text-area behavior.
- `/` prefix opens completion popup.
- `Ctrl+P` opens the searchable local/server command palette; long discovered
  command lists scroll with the keyboard selection kept visible.
- `Tab` accepts the highlighted completion.
- `Escape` clears input when idle and interrupts the active turn when running.
- `PageUp` and `PageDown` scroll the main transcript while the composer keeps
  keyboard focus.
- Idle submitted text appears immediately in the transcript.
- Submissions during an active turn are queued and drained FIFO.
- Queued follow-ups are visible in a compact Queue control above the composer.
  They enter the transcript only when their server turn starts. The control
  shows ordered message summaries and disappears as requests begin or finish;
  the server mailbox remains authoritative for delivery.
- During `user_input_required`, typed text is routed to the live interaction
  answer queue instead of starting a new user turn.
- During `permission_request`, typed approval shortcuts are routed to the
  permission response queue.
- Exiting during a blocking interaction cancels that turn. A later session
  resume displays the cancelled tool result and can start a new turn; the old
  interaction request itself cannot be answered after reconnect.

## Transcript Rendering

The transcript renders a flat chronological stream:

- user messages
- assistant messages and streaming deltas
- tool calls, tool deltas, and tool results
- usage updates
- client notices
- permission requests and decisions
- user-input requests and acknowledgements
- server command notices
- errors

There is no nested scroll region inside tool/message bodies. Long content stays
in the main transcript flow.

Assistant bodies render Markdown, including fenced code blocks, with the same
renderer used for live deltas and resumed history. User input, reasoning, tool
arguments, and tool results remain literal text so their exact payload is not
reinterpreted as presentation markup.

Reasoning and tool details are semantic controls rather than permanently
expanded log text:

- the assistant answer, tool name, key argument summary, status, and elapsed
  time remain visible
- reasoning is grouped under a collapsed `Thought` control
- complete tool arguments and results are grouped under a collapsed `Details`
  control; the header may expose one concise primary argument such as a
  command, path, query, or objective
- structured tool `data`, `error`, and `artifacts` remain available inside
  `Details`; only the compact summary is shortened
- streaming deltas update the existing control and preserve its expanded state
- `/thinking` and `/details` apply to existing and subsequently created controls
- clicking a control returns focus to the composer so the next keystroke is not
  lost

These controls use Textual's native `Collapsible`; they do not introduce a
second transcript model or alter the wire protocol.

Streaming tool indexes are local to one model response. Final
`assistant_message` and `tool_calls_started` events close that mapping so a
later tool batch in the same turn cannot reuse an earlier tool entry.

Streaming follows the transcript only while the user is already at the bottom.
When the user scrolls up, hidden reasoning or visible content updates must not
pull the viewport away from the inspected history.

## Live Interactions

### Permission And Sandbox

`permission_request` events render inline choices. The TUI supports:

- allow once
- allow for session
- deny once
- deny for session, when represented by the server command/policy layer

Responses are sent to:

```http
POST /sessions/{sid}/interactions/permission-response
```

The default keyboard parser accepts short forms such as `y`, `n`, and scoped
forms when exposed in the prompt. The HTTP response includes `scope` so
`allow session` and `always allow` can be persisted by the server policy layer.

### Ask User

`user_input_required` renders the question inline. The next submitted text is
sent to:

```http
POST /sessions/{sid}/interactions/user-input
```

The TUI must render acknowledgement events without duplicating the user's answer
as a new user turn. Each structured option shows its label and description and
submits the label. `Other` switches back to free text. The client does not add a
separate queued notice before the server acknowledgement arrives.

## Interrupt

`Escape` during a running turn calls:

```http
POST /sessions/{sid}/interrupt
```

The TUI treats idle interrupt as a no-op. If the server emits `turn_cancelled`,
status becomes `Interrupted` and the active-turn indicator clears.

## Status And Usage

The application header uses the product name `XBotv2`; internal client class
names are not user-visible.

The single-line status bar is the final screen row below the composer. It shows,
priority order:

- connection or run state and active-turn elapsed time; reasoning deltas show
  `Thinking`, while visible output and tool execution show `Running`
- queued message count when non-zero
- cumulative session token usage restored by `OpenSessionResponse` and updated
  from per-call `usage` events; the active-turn row remains turn-local
- `ctx-free` percentage, using the latest provider-reported effective context
  token count and the runtime `context_window`
- workspace, model, and provider from `OpenSessionResponse`
- session/thread identifiers only when the terminal is wide enough

Narrow terminals keep run state and token usage, then omit lower-priority
metadata. Context remaining is not derived from cumulative output: it compares
the latest request input against the configured runtime window and stays hidden
until the provider reports usage.

Server slash commands are displayed as local user transcript entries so the
operator can see what was executed. They remain command traffic and are not
added to model conversation history.

Usage updates must render before `turn_finished`; tests assert live status-bar
updates during a blocked turn.

The interaction model is informed by Codex's configurable status line and
OpenCode's reasoning/tool detail controls, but XBotv2 keeps a smaller fixed
surface until configuration has a concrete user requirement:

- <https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/status_line_setup.rs>
- <https://opencode.ai/docs/tui/>

## Transport Boundary

TUI modules must not import runtime-bound modules such as engine, bootstrap,
provider SDKs, or tool execution. The protocol boundary is:

```text
Textual app -> TerminalSession -> Transport -> HTTP server -> Engine
```

Boundary tests enforce this for TUI modules.

## Runtime Object Views

Background shell processes and subagents are displayed in a compact, collapsible Tasks
control with stable identifiers, status, elapsed time, command summary, and a
bounded result preview. `task_updated` replaces the existing snapshot in place
instead of appending transcript rows. The control keeps at most five task rows
visible while `/tasks` exposes the complete live-session list.

The lifecycle comes from the session-owned task manager through the existing
protocol envelope; the TUI does not infer it from tool text or mailbox
messages. The `kind` field distinguishes shell and Agent rows.

Tasks and queued follow-ups share one runtime band so they do not consume
independent vertical regions on short terminals. At less than 24 rows the band
keeps its titles and first summaries within four rows; status and composer never
overlap it.

## Trace And Diagnostics

`XBOTV2_TUI_TRACE` writes JSONL trace events for TUI state and HTTP transport
payloads. Trace output must preserve Unicode payloads.

HTTP transport errors parse stable JSON server errors before rendering them to
the user.

## Verification

Primary tests:

```bash
.venv/bin/python -m pytest XBotv2/tests/core/test_tui_client.py
.venv/bin/python -m pytest XBotv2/tests/integration/test_tui_interaction.py
.venv/bin/python -m pytest XBotv2/tests/integration/test_tui_interrupt_and_usage.py
.venv/bin/python -m pytest XBotv2/tests/integration/test_http_transport.py
```

Full runtime and TUI regression suite:

```bash
.venv/bin/python -m pytest XBotv2/tests/core XBotv2/tests/integration
```

The command result is the current baseline; do not preserve an old test count
as a completion claim when the suite changes.
