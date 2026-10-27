# Terminal UI

The TUI is a client-profile plugin that presents sessions, streams, commands, permission requests and user input through Textual. Its public `TerminalClient` contract and `TERMINAL_CLIENT` service key are declared in `api/service.py`: a client host obtains `ctx.require(TERMINAL_CLIENT)`, awaits `run()`, and can call `request_stop(reason)`.

The implementation depends on the HTTP client capability, command registry, shared wire contracts and Textual. It retains presentation state, current session and stream subscriptions for the client lifetime; it opens network requests through the client, renders to the terminal and registers local commands. The server owns session history, execution and interaction state.

`TextualTuiConfig` configures the workspace, session and thread, optional Agent, refresh interval and history display/retention limits. For example, a client profile with `workspace: .` and `thread_id: agent` opens the default workspace and Agent thread. `session_id` selects an existing session for resume; without one the client starts a new session.

The `/session` and `/thread` pickers load one page at a time. Its previous and next page rows navigate the catalogue; the filter applies to the current page.

History reading keeps one archive page separate from the live timeline. PageUp and PageDown read earlier and later pages, and PageDown at the newest page or Ctrl+End returns to live output. Completed entries outside `history_retention` or an initial 8 MiB estimated text budget are released, and older entries remain available through the archive. Archived message fields use bounded previews. Expanding a long block fetches one 4,096-character page; use Ctrl+Left and Ctrl+Right for adjacent pages. Collapsing or removing it releases the loaded page. A live stream is previewed until its canonical record is available.

Text tool-result artifacts appear as separate expandable blocks. Their pages use byte offsets, while conversation text fields use character offsets. Text artifacts decode as UTF-8 with replacement for invalid bytes; the original artifact remains available unchanged for download. Non-text artifacts remain metadata.

The transcript identifies image and non-text artifact references even when an input has no text. In-flight submissions are tracked by input ID; once accepted, queued or steered inputs are shown by the server-owned queue until consumed. `/copy` reads a completed reply's full canonical content on demand when the timeline holds only its preview.
