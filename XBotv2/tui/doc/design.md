# Terminal UI

The TUI is a client-profile plugin that presents sessions, streams, commands, permission requests and user input through Textual. Its public `TerminalClient` contract and `TERMINAL_CLIENT` service key are declared in `api/service.py`: a client host obtains `ctx.require(TERMINAL_CLIENT)`, awaits `run()`, and can call `request_stop(reason)`.

The implementation depends on the HTTP client capability, command registry, shared wire contracts and Textual. It retains presentation state, current session and stream subscriptions for the client lifetime; it opens network requests through the client, renders to the terminal and registers local commands. The server owns session history, execution and interaction state.

`TextualTuiConfig` configures the workspace, session and thread, optional Agent, refresh interval and history display/retention limits. For example, a client profile with `workspace: .` and `thread_id: agent` opens the default workspace and Agent thread. `session_id` selects an existing session for resume; without one the client starts a new session.

The TUI runs inline by default, leaving the terminal's earlier content and the final TUI display in place. It uses up to 24 rows, capped by the terminal height. Set `fullscreen: true` in the client profile or pass `--fullscreen` to use the alternate screen; `--inline` overrides a fullscreen profile. Textual's native inline driver is not available on Windows: use `--fullscreen` there. See the [Textual inline guide](https://textual.textualize.io/guide/app/#run-inline).

The interface follows the terminal's foreground and background colors. Conversation turns use compact markers, tools and reasoning remain foldable, and the composer has a single separator. Markdown code uses ANSI syntax colors without a contrasting background block.

Approvals, questions, and selection lists appear inline in the TUI. Ctrl+Q opens queued inputs: choose an input to edit its text in the composer, remove it, or steer it. While editing, Enter saves and Esc cancels; the prior composer draft is restored. Inputs reporting artifacts cannot be edited because the queue does not expose their original text and file identities. Ctrl+R recalls a loaded, accepted user message into a new draft; history is unchanged and files must be reattached. If the full text is externalized, recall reads it before replacing the draft. A change of thread or draft during that read cancels recall.

The `/session` and `/thread` pickers load one page at a time. Its previous and next page rows navigate the catalogue; the filter applies to the current page.

History reading keeps one archive page separate from the live timeline. PageUp and PageDown read earlier and later pages, and PageDown at the newest page or Ctrl+End returns to live output. Completed entries outside `history_retention` or an initial 8 MiB estimated text budget are released, and older entries remain available through the archive. Archived message fields use bounded previews. Expanding a long block fetches one 4,096-character page; use Ctrl+Left and Ctrl+Right for adjacent pages. Collapsing or removing it releases the loaded page. A live stream is previewed until its canonical record is available.

Text tool-result artifacts appear as separate expandable blocks. Their pages use byte offsets, while conversation text fields use character offsets. Text artifacts decode as UTF-8 with replacement for invalid bytes; the original artifact remains available unchanged for download. Non-text artifacts remain metadata.

The transcript identifies image and non-text artifact references even when an input has no text. In-flight submissions are tracked by input ID; once accepted, queued or steered inputs are shown by the server-owned queue until consumed. `/copy` reads a completed reply's full canonical content on demand when the timeline holds only its preview.

Images can be attached to a draft via `/attach <path>`; recall restores text only. The client does not provide an external-editor command or an atomic operation to replace a message that may already have been consumed. Queue edits remain subject to the server's current queue state.

The following comparison describes only features established by the cited sources, not overall equivalence:

| Area | XBot Textual client | Local C++ client / Maya | Official Codex CLI |
| --- | --- | --- | --- |
| Terminal display | Inline by default; optional fullscreen | Config selects Maya inline or fullscreen mode ([loop.cpp](../../../../XBot_cpp/xbot/tui/src/loop.cpp), [api.hpp](../../../../XBot_cpp/xbot/tui/include/xbot/tui/api.hpp)) | `/tui` switches between scrollback and fullscreen ([commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)) |
| Appearance | Terminal colors and ANSI code highlighting | Not compared here | `/theme` selects a syntax highlighting theme ([commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)) |
| Editing | Inline queue editing and loaded-history recall as a new draft; artifact-bearing queue edits are unavailable | Model includes a queue editor and draft history ([app.hpp](../../../../XBot_cpp/xbot/tui/include/xbot/tui/app.hpp)) | Ctrl+G opens the current prompt in an external editor ([customization](https://learn.chatgpt.com/docs/cli-customization)) |
| History | Archive pages and on-demand body pages | Not compared here | Not compared here |

The C++ references were inspected at `851b9367f5c09136477691f723fa1d2a43a204f8`. Maya is a C++ library used by that client; using it as another native client for Python XBot would require adapting the Python wire contracts and providing build artifacts. This client uses Textual's native inline and theme support and adds no Maya binding.
