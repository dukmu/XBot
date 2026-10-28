# Terminal UI

The TUI is a client-profile plugin that presents sessions, streams, commands, permission requests and user input through Textual. Its public `TerminalClient` contract and `TERMINAL_CLIENT` service key are declared in `api/service.py`: a client host obtains `ctx.require(TERMINAL_CLIENT)`, awaits `run()`, and can call `request_stop(reason)`.

The implementation depends on the HTTP client capability, command registry, shared wire contracts and Textual. It retains presentation state, current session and stream subscriptions for the client lifetime; it opens network requests through the client, renders to the terminal and registers local commands. The server owns session history, execution and interaction state.

`TextualTuiConfig` configures the workspace, session and thread, optional Agent, refresh interval and history display/retention limits. For example, a client profile with `workspace: .` and `thread_id: agent` opens the default workspace and Agent thread. `session_id` selects an existing session for resume; without one the client starts a new session.

The TUI uses the full terminal by default and adapts when the terminal is resized. Set `fullscreen: false` in the client profile or pass `--inline` to retain the display in terminal scrollback. Textual’s inline driver is unavailable on Windows.

Conversation turns use compact markers, tools and reasoning remain foldable, and the composer stays compact. Markdown code uses ANSI syntax colors without a contrasting background block.

Approvals, questions, and selection lists appear inline in the TUI. Ctrl+Q opens queued inputs: choose an input to edit its text in the composer, remove it, or steer it. While editing, Enter saves and Esc cancels; the prior composer draft is restored. Inputs reporting artifacts cannot be edited because the queue does not expose their original text and file identities. Ctrl+R recalls a loaded, accepted user message into a new draft; history is unchanged and files must be reattached. If the full text is externalized, recall reads it before replacing the draft. A change of thread or draft during that read cancels recall.

The `/session` and `/thread` pickers load one page at a time. Its previous and next page rows navigate the catalogue; the filter applies to the current page.

The command palette and completion consume the server's published command
catalog. Business commands, aliases, and prompt commands dispatch through the
generic client contract; adding a server command requires no TUI handler. The
TUI registers only its local view and terminal actions. Model, provider and
Agent selection use commands published by their respective owners.

Scrolling to either end loads adjacent history as needed while retaining the visible boundary; Ctrl+End returns to live output. The mounted window and archive residency are bounded, and older entries remain available through the archive. An estimated 8 MiB text budget applies to retained entries, with room for the boundary and an adjacent entry when either is unusually large. Archived fields use bounded previews; expanding a field reads its full content on demand and collapsing or removing it releases that content. Live assistant text and reasoning are retained in full. Expanded blocks scroll with the transcript.

Text tool-result artifacts appear as separate expandable blocks. Their pages use byte offsets, while conversation text fields use character offsets. Text artifacts decode as UTF-8 with replacement for invalid bytes; the original artifact remains available unchanged for download. Non-text artifacts remain metadata.

The transcript identifies image and non-text artifact references even when an input has no text. In-flight submissions are tracked by input ID; once accepted, queued or steered inputs are shown by the server-owned queue until consumed. `/copy` reads a completed reply's full canonical content on demand when the timeline holds only its preview.

Images can be attached to a draft via `/attach <path>`; recall restores text only. The client does not provide an external-editor command or an atomic operation to replace a message that may already have been consumed. Queue edits remain subject to the server's current queue state.

The settings panel discovers configuration resources and available scopes from the server. JSON resources with object schemas use field controls, with a full JSON editor available for other shapes and unknown fields; YAML and Markdown resources use a text editor. The resource description explains when changes take effect. Saves use the server's revision and validation; on conflict the draft remains visible until the user explicitly discards it and reloads. Read-only thread views cannot save.
