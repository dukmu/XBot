# Textual TUI closure evidence

This note records the finite product checks used to close the current Textual
TUI plan. It is evidence for the listed behavior, not a replacement for the
public API contracts or a claim that every terminal and provider was tested.

| Area | Observable evidence |
| --- | --- |
| Reconnect | A real `xbot tui` process, uvicorn server, and TCP forwarder cut the live socket during reasoning, a permission dialog, a question dialog, idle state, and a session switch. The same pending interaction or transcript returned once, selection and composer focus remained usable, and follow-up input completed. |
| Cursor expiry | Completed turns sent through `XBotClient` naturally overflow the bounded SSE replay window; subscribing with the original public `after` cursor returns retryable `session_event_cursor_expired`. Transport tests cover the bounded rewind and baseline rebuild paths. |
| Long history | A test-only `MockLLM` subclass gates turn 31 after its first stream event. At 80x24 and 100x28, turn 0 remains anchored while status is `Running`; completion also preserves the anchor. Returning to the tail shows turn 31. This check exposed and fixed an unconditional submit-to-tail jump. |
| Compact rendering | Current tmux captures cover permission and question dialogs, folded/expanded Think and tool rows, multiline paste, session switch, and long Unicode tool arguments/results at 80x24. The Unicode cases check every captured row against the terminal cell width; canonical public history retains the tool output. |
| Settings | Existing public APIs cover provider/model/effort/agent selection and workspace plugin configuration. Plugin writes carry the catalog revision; after a 409 conflict Settings reloads the catalog, keeps the edited draft, and a second Apply writes with the new revision. Leaving Settings restores the composer draft and focus. Permission and sandbox policy are displayed read-only because the public API exposes no mutation here. Appearance is process-local and unavailable theme choices are not fabricated. |
| Partial failure and interrupt | Real uvicorn plus Textual pilot exercises reasoning/text followed by a provider failure, Escape after the first reasoning delta, and shell output followed by exit 7. Partial content and the error/cancellation remain visible, Running ends, and a later submission succeeds. The tool error is also checked against public canonical history. These are rendered pilot tests, not PTY cases or external-provider interoperability. |
| Cleanup | PTY cases leave through the normal quit path and fixture teardown closes proxy relays, clients, server tasks, and tmux sessions. |

The final integrated run used code commit `8d61769`: TUI/ACP 903 passed in
165.00 seconds. Captures are under `/tmp/xbot-final-target-20260929` and are
intentionally not committed. The primary reviewer read the current reconnect,
history-anchor, and Unicode captures and rendered all three failure/interrupt
SVG screenshots locally. The question reconnect frame retains the selected
Tuesday row; the history frame shows turn 0 while turn 31 is Running; the
Unicode case keeps args, result, and final reply operable at 80 columns.

Not claimed here: paid-provider interoperability, an exhaustive terminal
emulator matrix, or Settings mutations for capabilities that have no public
write API.

## 2026-09-30 rendering follow-up

The transcript now reuses Rich Markdown segments between measurement and
painting of the same document at the same width, instead of rendering it twice.
The cache is local to that version of the renderable, not a growing conversation
cache. Width changes invalidate it. Tests compare the exact styled segments
against ordinary Rich Markdown, including nested lists, links, quotes, tables,
Unicode code highlighting, and resize. Reasoning-only updates leave the answer
renderable untouched. Unchanged status, hints, queue/job rows and tool disclosure
contents do not invalidate layout; an unchanged transcript does not reschedule
tail scrolling. History-notice changes still trigger scroll reconciliation.

A same-process Textual pilot comparison at 100x30, with a synthetic 20,000-character
Markdown answer and six five-character updates, measured median update-and-settle
time of 330.5 ms without the Markdown reuse and 260.0 ms with it. These timings
include pilot scheduling and are indicative, not a terminal latency guarantee.
Full-document parsing/layout still costs time for very large single answers;
this change does not claim constant-cost streaming or universally smooth scrolling.

Real local-server/PTY checks revalidated MiniMax-format thinking at three sizes,
two-client session isolation and disk resume, and removal of the pending-input
marker after acknowledgement. MiniMax here is the test SSE endpoint, not a paid
external-provider request. Provider timeout behavior is separately verified with
real OpenAI and Anthropic SDK HTTP reads; see [getting started](getting-start.md)
for timeout and retry configuration semantics.

## Markdown selection and row rendering

The current view prepares Rich Markdown and its width-specific display rows
off the UI loop. Painting requests individual rows instead of splitting the
whole document again. Width changes prepare new rows in a Textual worker;
the old content remains visible while that work completes. Both the segment
and row caches retain at most two widths per renderable.

The display rows carry Textual selection offsets. Mouse selection and Ctrl+C
copy rendered text, including code and Chinese characters; selection highlighting
is applied after Markdown styling. Speaker markers are added to display rows,
not to Markdown source, so the first heading is parsed correctly.
Tool parameters/results use YAML literal blocks for multiline structured values.
Strings are never unescaped a second time; literal backslash-n and real newlines
remain distinct. Tool windows wrap to their width and retain their height cap.
Ctrl+O folds/unfolds blocks; Ctrl+E retains the editor's line-end operation.
Shift+Enter replaces selected input with a newline and leaves the caret after it.

The implementation review consulted
[Codex streaming](https://github.com/openai/codex/blob/main/codex-rs/tui/src/streaming/controller.rs),
[OpenTUI Markdown](https://github.com/anomalyco/opentui/blob/main/packages/core/src/renderables/Markdown.ts),
and [Glamour](https://github.com/charmbracelet/glamour).
Codex/OpenTUI distinguish stable content from a mutable streaming tail;
that incremental parser behavior is not implemented by this row cache.
The [Codex math renderer](https://github.com/openai/codex/blob/main/codex-rs/tui/src/markdown_render/math/render.rs)
supports a bounded TeX subset through Unicode layouts; XBot does not yet add
formula or diagram layout.

A local 80x24 Textual experiment used synthetic 20,000/80,000-character
documents with repeated headings, lists, Chinese prose and code fences.
A 10 ms asyncio heartbeat measured maximum scheduling gaps:

| Rendering path | 20k characters | 80k characters |
| --- | --- | --- |
| Rich segments in ordinary Static | 84.5 ms | 747.5 ms |
| Textual native Markdown | 759.6 ms | 3069.0 ms |
| Prepared rows, visible-row painting | 36.2 ms | 98.7 ms |
| Prepared rows, resize 80 to 60 columns | 27.1 ms | 98.2 ms |

Native Markdown created approximately 2,600/10,400 widgets for these documents.
These are same-machine diagnostic samples, not CI timing thresholds.
The row path still parses each updated document, and large Python preparation
can contend for the GIL. Tests instead verify visible input while preparation
is delayed, scrolling during preparation, background resize, mouse copying,
and 40/80-column tool payloads without machine-dependent timing assertions.

Verification on this change: 813 core/integration cases, 888 non-server TUI
cases, 208 focused app/entry/transcript cases including the added Chinese mouse
copy case, and all 49 real-server/CLI/PTY cases passed. The interaction sequence
exposed a preparation defect: a full window remount also needs unchanged entries,
not only entries with changed text. A regression now checks a replaced middle
record retains both neighboring rows, and the real permission/question flow
checks the initial prompt remains mounted and visible after resize/disclosure.
Current terminal captures are under `/tmp/xbot-markdown-final-20260930/`.
The original prompt, resolved permission/question rows, Think block and final
reply were read from the new idle-reconnect capture, not a previous artifact.
