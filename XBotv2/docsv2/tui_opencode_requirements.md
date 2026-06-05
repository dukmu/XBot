# XBotv2 TUI OpenCode-Style Requirements and Design Report

Status: draft for correction before further implementation.

This report is the source of truth for the next TUI iteration. The current
Textual implementation is not considered accepted. Passing unit tests are not
enough because the reported failures are visible only in real terminal use.

## 1. Scope

The target is the XBotv2 Textual TUI only:

- Runtime entry: `xbotv2 --mode tui`
- Client implementation: `xbotv2/tui/textual_client.py`
- Protocol transport: `xbotv2/tui/terminal.py`
- Replayable TUI state: `xbotv2/tui/client.py`
- Protocol contract: `xbotv2/protocol/*`

Legacy `xbot/` and root-level deprecated entry points are out of scope.

## 2. OpenCode-Style Product Requirements

### R1. One Main Event Stream

Goal:

- The visible UI is one chronological event stream.
- User messages, assistant messages, tool calls, tool results, permission
  requests, sandbox requests, ask-user prompts, client messages, and local
  acknowledgements all appear inline in that stream.
- There is no right-side panel, no separate tool/event pane, and no fixed
  option panel.

Current evidence:

- `textual_client.py` renders `#transcript` plus a bottom composer.
- Tool and notice widgets are mounted into `#transcript`.

Current gap:

- Event ordering is still fragile because activity widgets are mounted outside
  `state.transcript`, while messages/tools/notices are mounted from transcript
  entries. This creates two ordering mechanisms.
- The user's real output shows duplicated `approval queued` and blank assistant
  sections, so the event stream is not yet a trustworthy representation of the
  protocol stream.

Design:

- Use one append-only render log as the only source for visible stream rows.
- Convert activity rows, local acknowledgements, and protocol events into the
  same render-log model.
- Do not mount stream widgets from multiple independent paths.

Acceptance:

- A captured real TUI session must show exact chronological order for:
  `user -> turn_started/activity -> assistant/tool request -> permission choice
  -> response recorded -> tool result -> final assistant -> turn finished`.
- No duplicated local acknowledgement for one user action.

### R2. Single Interaction Mode

Goal:

- There is never a separate focusable message stream and focusable input area.
- In normal mode, the composer is the only text input.
- In choice mode, the composer is hidden/disabled and the keyboard operates the
  inline choice in the stream.

Current evidence:

- Current worktree introduces `TranscriptScroll(can_focus = False)`.
- Current worktree hides/disables `#input` during active inline choices.

Current gap:

- The design is not fully proven in real terminal behavior.
- Current tests assert internal widget flags, but do not verify real rendered
  terminal frames across the complete protocol lifecycle.

Design:

- Define explicit UI modes:
  - `COMPOSING`: bottom composer visible and focused.
  - `QUEUED`: composer visible but send creates queued message if a turn is
    active.
  - `CHOOSING`: composer hidden; Up/Down moves selected inline choice; Enter
    submits it.
  - `WAITING_FOR_ACK`: composer hidden after choice submission until the server
    acknowledges or the turn ends.
- Only `COMPOSING` and `QUEUED` accept free-form text.

Acceptance:

- In choice mode, the rendered frame contains no visible input box.
- Typing letters in choice mode must not alter hidden composer state.
- Up/Down/Enter must work even when no widget is focused.

### R3. Keyboard Behavior

Goal:

- Normal mode:
  - Enter sends.
  - Shift+Enter inserts a newline.
  - Up/Down navigates input history when appropriate.
- Choice mode:
  - Up/Down moves selection.
  - Enter confirms selection.
  - Free typing is ignored.

Current evidence:

- `ComposerTextArea._on_key` handles Enter, Shift+Enter, and history.
- `XBotTextualApp.on_key` handles choice mode keys in the current worktree.

Current gap:

- There is still residual choice handling inside `ComposerTextArea`; this is
  conceptually wrong if the composer is hidden during choice mode.
- Repeated Enter appears able to produce duplicate `approval queued` in real
  output. Confirmation must be idempotent by request id.

Design:

- Composer owns normal mode only.
- App-level key handler owns choice mode only.
- Choice confirmation must atomically transition the request id into
  `submitted` before any await point.

Acceptance:

- Pressing Enter multiple times for one permission request emits exactly one
  `permission.response`.
- The stream contains at most one local acknowledgement for one request id.

### R4. Unicode and Chinese Input

Goal:

- Chinese typed in the terminal is preserved from Textual input through:
  UI state, protocol `user.message`, persisted events, model prompt, and
  rendered transcript.

Current evidence:

- Unit tests prove that manually loaded Unicode strings render correctly in
  headless Textual.
- Real terminal output shows mojibake:
  `å½åç£çç¨äºå¤å°`

Current gap:

- The bug is not a Rich rendering issue alone. The user message is already
  corrupted by the time it is displayed as `You`.
- Existing tests bypass the real terminal input path by calling
  `input_widget.load_text("你好")`.

Design:

- Add a real terminal/input-path diagnostic before applying fixes:
  - Log raw `TextArea.text` repr at submit time.
  - Log outgoing JSON frame payload repr.
  - Log server-received `user.message` payload repr.
- Verify locale and Textual driver behavior in the same shell used to run TUI.
- Do not add blind mojibake "repair" as the primary fix; that can corrupt valid
  non-Chinese input. Only use repair as a guarded fallback if the root cause is
  proven to be unrecoverable terminal decoding.

Acceptance:

- A real TUI session where the user types `当前磁盘用了多少` must render that exact
  string in the `You` message and send that exact string in the JSONL frame.

### R5. No Message Swallowing

Goal:

- Every non-empty user and assistant message emitted by the protocol appears
  visibly in the stream.
- Empty or whitespace-only assistant messages should not render as a blank
  titled block.

Current evidence:

- `TuiState.apply_event` currently appends assistant messages when `content` is
  truthy, but whitespace-only content is truthy.
- Real output shows blank assistant blocks:
  - `16:46:31  XBotv2` followed by no assistant body.
  - `16:47:01  XBotv2` followed by no visible body.
- User reports messages are still being swallowed.

Current gap:

- Existing tests only check simple fake assistant events and do not replay a
  real LM Studio tool-use sequence.
- There is no raw protocol capture correlated with the rendered frame, so it is
  not yet known whether the message is lost in protocol, state, widget mount, or
  terminal rendering.

Design:

- Add a TUI debug capture mode that records:
  - raw protocol event sequence,
  - state transcript entries after each event,
  - rendered widget keys,
  - exported SVG/text frame after each event in test mode.
- Treat assistant messages with `content.strip() == ""` as non-renderable, while
  still processing tool calls.
- Ensure Rich markup is disabled for message bodies and tool summaries.
- Replace mixed activity/transcript mounting with the single render log from R1.

Acceptance:

- Given a captured real protocol event sequence, replaying it into TUI state and
  exporting a frame must show all non-empty assistant messages exactly once.
- Whitespace-only assistant messages must not create blank `XBotv2` blocks.

### R6. Turn Serialization and Queueing

Goal:

- A user can type while the agent is running.
- New messages are shown as queued, not inserted as normal user turns.
- After the active turn ends, queued messages are sent in FIFO order.

Current evidence:

- `queue_user_message` queues normal messages.
- `_drain_message_queue` appends a user message only when consumed.
- Status bar shows `queued:N`.

Current gap:

- There is no clear stream row for queued messages.
- The old visual bug `user-user-ai-ai` must be tested in real rendering, not
  only state order.

Design:

- Add explicit queued rows in the event stream:
  - `queued  HH:MM:SS  You  <content>`
  - When consumed, update or replace the queued row with normal `You`.
- Preserve FIFO ordering in both state and visual stream.

Acceptance:

- Sending two messages during one active turn renders:
  active user message, assistant stream, queued user row, then next turn user
  row after first turn completes.

### R7. Status Bar

Goal:

- Status information is one compact top or bottom status bar.
- It must not occupy a right panel.
- It shows session/thread, agent, turn state, queue depth, elapsed time, and
  usage.

Current evidence:

- Current status bar shows session/thread, agent, turn, queue depth, and usage.

Current gap:

- Usage is total-only in the bar; per-turn usage is in activity row.
- Activity elapsed time currently depends on activity widgets outside the main
  transcript log.

Design:

- Keep status compact.
- Show current turn elapsed and total usage in status.
- Show per-turn elapsed/usage in the turn activity stream row.

Acceptance:

- LM Studio usage frames must update status in the same run where usage is
  returned by the provider.

### R8. Mouse Scrolling and Copyability

Goal:

- Mouse wheel scrolls the transcript.
- The UI should not prevent terminal text selection/copy more than Textual
  inherently requires.

Current evidence:

- `VerticalScroll` provides scroll behavior.
- `TranscriptScroll(can_focus = False)` preserves mouse scrolling in principle.

Current gap:

- Terminal mouse capture may prevent native selection depending on Textual
  mouse mode and terminal emulator.
- No manual verification result is recorded for copy behavior.

Design:

- Keep transcript mouse-scrollable but non-focusable.
- Investigate Textual mouse mode settings and terminal emulator behavior.
- If native selection cannot coexist with mouse wheel in Textual, document the
  tradeoff and provide an alternate copy mode later.

Acceptance:

- Manual run verifies mouse wheel scroll.
- Manual run records whether native terminal selection works in the target
  terminal.

### R9. Visual Style

Goal:

- Dense, quiet, event-stream TUI similar to OpenCode.
- No right-side panels, no card-heavy layout, no large help regions.
- Timestamps are visible per row.
- Activity is dynamic but compact.

Current evidence:

- Current CSS uses a dark event-stream layout with timestamps in meta rows.

Current gap:

- The visible output still has excessive vertical gaps and blank assistant
  sections.
- Tool result preview is compressed into one line and may be too hard to read.

Design:

- Use compact row blocks:
  - metadata line,
  - body line(s),
  - optional compact details.
- Collapse empty bodies.
- Tool details should be expandable later, but the default should show enough
  context without overwhelming the stream.

Acceptance:

- A real frame with user, assistant, tool, permission, and final assistant must
  fit coherently in a standard 100x32 terminal.

## 3. Current High-Priority Defects

1. Chinese input is corrupted in real TUI input.
   - Severity: blocker.
   - Evidence: user-provided output shows mojibake in the `You` row.

2. Assistant messages are still swallowed or rendered as blank blocks.
   - Severity: blocker.
   - Evidence: user report plus blank `XBotv2` rows in real output.

3. Permission acknowledgement can duplicate.
   - Severity: high.
   - Evidence: user output contains two identical `approval queued` rows for
     one selected permission.

4. Tests are too weak.
   - Severity: high.
   - Evidence: headless tests pass while real TUI fails.
   - Missing coverage: real terminal input path, real protocol replay, duplicate
     Enter idempotency, LM Studio tool-use transcript replay.

5. Rendering model is split.
   - Severity: high.
   - Evidence: activity widgets are mounted independently from transcript
     entries.

## 4. Proposed Architecture

### 4.1 Protocol Event Log

Keep `TerminalSession` as protocol-only. Every server frame should be available
to the TUI as raw event data before any UI transformation.

### 4.2 UI Render Log

Introduce a dedicated Textual render model:

```text
RenderEntry
  id: stable string
  kind: user | assistant | tool | permission | ask_user | local_ack | activity | error
  request_id: optional protocol request id
  turn: optional turn id
  timestamp: local display timestamp
  status: pending | active | submitted | resolved | failed
  body: display text
  choices: optional list
  selected_index: optional int
```

All visible stream rows come from this model. No widget should be mounted
outside it.

### 4.3 Interaction Controller

Track one active interaction:

```text
ActiveInteraction
  request_id
  kind: permission | ask_user | sandbox
  choices
  selected_index
  state: choosing | submitted
```

Rules:

- There can be only one active interaction.
- Confirming sets `state=submitted` synchronously before awaiting.
- A second Enter for the same `request_id` is ignored.
- Server ack resolves the interaction and restores composer mode.

### 4.4 Input Controller

The composer is enabled only when the UI mode accepts text. It must not try to
handle choice mode.

### 4.5 Renderer

Renderer consumes the render log and updates/mounts widgets by stable entry id.
It must support update-in-place for:

- activity elapsed time,
- queued to active message transition,
- choice selected index,
- choice submitted/resolved state,
- tool result completion.

## 5. Verification Plan

Minimum required checks before claiming OpenCode-style TUI is fixed:

1. Unit: state ignores whitespace-only assistant content but preserves tool
   calls.
2. Unit: one permission request plus repeated Enter emits one response and one
   local ack.
3. Unit: choice mode hides/disables composer and ignores free typing.
4. Unit: render log preserves event order under user/tool/permission/final
   assistant sequence.
5. Replay: feed captured LM Studio protocol events into Textual headless app and
   export SVG; assert all non-empty messages are visible.
6. Manual: run real server and TUI, type Chinese text directly, verify no
   mojibake in UI and JSONL payload.
7. Manual: run tool permission flow, select `Always allow`, verify no duplicate
   ack and no blank assistant blocks.
8. Manual: verify mouse wheel scrolls transcript.

## 6. Non-Goals for the Immediate Fix

- Do not redesign server/session/thread semantics.
- Do not touch legacy `xbot/`.
- Do not add right panels or fixed option areas.
- Do not add broad keyboard shortcuts beyond required input/history/choice
  behavior.
- Do not solve terminal-native copy if it conflicts with Textual mouse support
  until mouse/copy behavior is explicitly tested.

## 7. Implementation Order

1. Add debug/replay instrumentation for real protocol and rendered stream.
2. Fix render log architecture so all rows share one ordered source.
3. Fix choice controller idempotency and remove choice handling from composer.
4. Filter whitespace-only assistant blocks.
5. Diagnose Chinese input at the real Textual input boundary.
6. Replace weak headless tests with protocol replay and SVG/text-frame checks.
7. Run manual TUI verification and record results.

## 8. Current Worktree Note

At the time this report was written, the worktree already contained uncommitted
changes in:

- `XBotv2/xbotv2/tui/textual_client.py`
- `XBotv2/tests/core/test_tui_client.py`
- `XBotv2/data/personalities/default/personality.yaml`
- `main.py`

The TUI code changes are not accepted as complete. The personality and root
`main.py` changes are unrelated to this report and should not be included in
TUI stabilization commits unless explicitly reviewed.
