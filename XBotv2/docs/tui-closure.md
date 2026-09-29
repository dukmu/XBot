# Textual TUI closure evidence

This note records the finite product checks used to close the current Textual
TUI plan. It is evidence for the listed behavior, not a replacement for the
public API contracts or a claim that every terminal and provider was tested.

| Area | Observable evidence |
| --- | --- |
| Reconnect | A real `xbot tui` process, uvicorn server, and TCP forwarder cut the live socket during reasoning, a permission dialog, a question dialog, idle state, and a session switch. The same pending interaction or transcript returned once, selection and composer focus remained usable, and follow-up input completed. |
| Cursor expiry | Completed turns sent through `XBotClient` naturally overflow the bounded SSE replay window; subscribing with the original public `after` cursor returns retryable `session_event_cursor_expired`. Transport tests cover the bounded rewind and baseline rebuild paths. |
| Long history | A test-only `MockLLM` subclass gates turn 31 after its first stream event. At 80x24 and 100x28, turn 0 remains anchored while status is `Running`; completion also preserves the anchor. Returning to the tail shows turn 31. This check exposed and fixed an unconditional submit-to-tail jump. |
| Compact rendering | Current tmux captures cover permission and question dialogs, folded/expanded Think and tool rows, multiline paste, session switch, and long Unicode tool arguments/results at 80x24. Every captured row is checked against the terminal cell width; canonical public history retains the Unicode tool output. |
| Settings | Existing public APIs cover provider/model/effort/agent selection and workspace plugin configuration. Plugin writes carry the catalog revision; a 409 conflict keeps the edited draft, and leaving Settings restores the composer draft and focus. Permission and sandbox policy are displayed read-only because the public API exposes no mutation here. Appearance is process-local and unavailable theme choices are not fabricated. |
| Cleanup | PTY cases leave through the normal quit path and fixture teardown closes proxy relays, clients, server tasks, and tmux sessions. |

The stable local captures for the focused closure run are under
`/tmp/xbot-tui-product-closure-focused`. They are intentionally not committed:
the question reconnect frame shows one question and the selected Tuesday row;
the streaming history frames show turn 0 at both terminal sizes while turn 31
is running; and the Unicode expanded frame remains 80 cells wide.

Not claimed here: paid-provider interoperability, an exhaustive terminal
emulator matrix, or Settings mutations for capabilities that have no public
write API.
