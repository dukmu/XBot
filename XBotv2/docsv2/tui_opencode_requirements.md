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
- `/clear`: clear visible transcript state without changing the server session
- `/help`: render all known local and server command labels

Server commands discovered from `GET /commands`:

- `/status`
- `/provider status|list|use <name>`
- `/permission status|list|set <tool> <allow|deny|ask>|reset [tool]`
- `/sandbox status|list|set <key> <allow|readwrite|readonly|deny|ask>|reset [key]`

Slash dispatch rules:

- local commands execute locally
- registered server commands call `POST /sessions/{sid}/commands`
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
- `Tab` accepts the highlighted completion.
- `Escape` clears input when idle and interrupts the active turn when running.
- Submitted text appears immediately in the transcript before server response.
- Submissions during an active turn are queued and drained FIFO.
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
as a new user turn. An inline selection is shown on its request widget; free
text uses the waiting composer state. The client does not add a separate queued
notice before the server acknowledgement arrives.

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

The status bar shows:

- connection/session state
- current mode
- queued message count
- provider/session identifiers when available
- realtime token usage from `usage` events

Usage updates must render before `turn_finished`; tests assert live status-bar
updates during a blocked turn.

## Transport Boundary

TUI modules must not import runtime-bound modules such as engine, bootstrap,
provider SDKs, or tool execution. The protocol boundary is:

```text
Textual app -> TerminalSession -> Transport -> HTTP server -> Engine
```

Boundary tests enforce this for TUI modules.

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
