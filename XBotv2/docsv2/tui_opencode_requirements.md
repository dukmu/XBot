# XBotv2 TUI Requirements and Design Report, Based on OpenCode

Status: evidence-backed draft. This supersedes the previous report, which was
not acceptable because it did not first inspect OpenCode's official docs and
repository.

Last reviewed: 2026-06-05.

## 1. Sources Checked

Primary sources:

- OpenCode TUI docs: `https://opencode.ai/docs/tui/`
- OpenCode keybind docs: `https://opencode.ai/docs/keybinds/`
- OpenCode permission docs: `https://opencode.ai/docs/permissions/`
- OpenCode GitHub TUI entrypoint:
  `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/app.tsx`
- OpenCode raw TUI source:
  `https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/cli/cmd/tui/app.tsx`

Facts from those sources:

- OpenCode provides an interactive terminal UI for working with an LLM, and
  running `opencode` starts the TUI for the current directory.
- OpenCode TUI supports slash commands, file references, shell-command messages,
  model/session/theme/status dialogs, and configurable `tui.json`.
- OpenCode keybind defaults include:
  - `input_submit = return`
  - `input_newline = shift+return, ctrl+return, alt+return, ctrl+j`
  - `history_previous = up`, `history_next = down`
  - `dialog.select.prev = up`, `dialog.select.next = down`,
    `dialog.select.submit = return`
  - message scrolling commands exist, including PageUp/PageDown, but the
    current XBotv2 requirement explicitly asks for mouse wheel rather than
    PageUp/PageDown.
- OpenCode TUI config includes `mouse`; the docs state that disabling mouse
  preserves native terminal mouse selection and scrolling behavior.
- OpenCode permissions are configured as `allow`, `ask`, or `deny`.
- When OpenCode asks for approval, the UI offers `once`, `always`, and `reject`.
- OpenCode permissions include tool-like domains such as `read`, `edit`, `bash`,
  `question`, `webfetch`, `websearch`, and `external_directory`.
- OpenCode's TUI source uses OpenTUI/Solid, plugin slots, dialog providers,
  route providers, a keymap provider, copy/selection utilities, a terminal
  renderer, and an explicit mouse-enabled renderer configuration.

Important correction:

- OpenCode is not just "a single transcript with no dialogs". It has dialogs,
  command palette, session/model/theme/status views, plugin routes, and
  configurable keymaps.
- The XBotv2 target is therefore not "clone every OpenCode UI behavior". The
  target is: adopt the OpenCode-style terminal-agent interaction model while
  preserving the explicit constraints already given for XBotv2.

## 2. XBotv2-Specific User Constraints

These are explicit local requirements and can intentionally differ from
OpenCode defaults:

- Work only on `XBotv2`; legacy `xbot/` is deprecated.
- TUI must be the XBotv2 TUI, not the old curses/legacy UI.
- OpenCode-like layout means event-stream first, not a right-side panel.
- Do not keep a right-side tool/event area.
- Do not use fixed button areas for permission/sandbox/ask-user choices.
- Do not create multiple keyboard focus regions between message stream and
  input.
- If a live choice is required, the input box should not render or be usable.
- Choice options are operated by Up/Down and confirmed with Enter.
- Normal composer supports Enter send, Shift+Enter newline, and Up/Down history.
- Mouse wheel scrolling is required. PageUp/PageDown is not a desired primary
  interaction.
- Chinese input must work.
- Messages must not be swallowed.
- Status should be a compact status bar, not a large right panel.
- Every message should show a timestamp.
- Every active turn should show dynamic working state, elapsed time, and token
  usage.
- Usage from LM Studio-compatible OpenAI responses should be displayed when the
  protocol emits usage.

## 3. OpenCode Feature Mapping to XBotv2

| Area | OpenCode evidence | XBotv2 target |
| --- | --- | --- |
| Terminal-first agent UI | Official TUI docs define terminal UI as the primary interactive surface. | Keep Textual TUI as the primary XBotv2 interactive surface. |
| Configurable keybinds | Official keybind docs define submit/newline/history/dialog selection keys. | Implement required key behavior first; config can come later. |
| Choice navigation | OpenCode dialog selection uses Up/Down/Return. | XBotv2 inline choices use Up/Down/Enter. |
| Permission ask outcomes | OpenCode docs define `once`, `always`, `reject`. | XBotv2 should expose `allow once`, `allow session/always`, and `deny`; naming must match runtime semantics. |
| Mouse behavior | OpenCode docs have `mouse` config; disabling mouse preserves native terminal behavior. | XBotv2 must support wheel scrolling and later decide/document native selection tradeoff. |
| Copy/selection | OpenCode source wires selection/copy utilities into renderer. | XBotv2 currently has no equivalent; copyability must be tested honestly. |
| Plugins | OpenCode TUI source has plugin runtime slots. | XBotv2 should keep TUI protocol-only and plugin-friendly, but not import runtime boundaries. |
| Dialogs | OpenCode uses dialogs extensively. | XBotv2 should not add fixed choice panels now; choices stay in event stream per user requirement. |

## 4. Required XBotv2 UI Model

### 4.1 Layout

Target:

- One main chronological event stream.
- One compact status bar.
- Bottom composer only when free-form input is accepted.
- No right-side panel.
- No persistent tool/event side panel.
- No fixed option strip outside the stream.

Rationale:

- OpenCode is terminal-first and has configurable TUI behavior.
- The user's XBotv2 constraint narrows this to a main event stream and compact
  status bar.

Current XBotv2 gap:

- Current implementation still has mixed render paths: protocol transcript
  entries are mounted via `_render_new_transcript_entries`, while activity rows
  are mounted separately. This makes ordering fragile.

Design:

- Introduce one UI render log for all visible rows:
  - user message
  - assistant message
  - tool call
  - tool result
  - permission request
  - sandbox request
  - ask-user request
  - local acknowledgement
  - turn activity
  - usage update
  - error/client message
- The Textual widget tree should be a projection of this render log.

### 4.2 Modes

Use explicit modes:

- `COMPOSING`: composer visible and usable.
- `RUNNING`: active turn in progress; composer can accept queued messages only
  if queueing is enabled.
- `CHOOSING`: live permission/sandbox/ask-user choice is active; composer is
  hidden and disabled.
- `SUBMITTED`: a choice was submitted; composer remains hidden until server
  acknowledgement or turn end.
- `ERROR`: composer state depends on recoverability.

Mode rules:

- In `CHOOSING`, Up/Down changes the selected inline option and Enter confirms.
- In `CHOOSING`, text keys must not change hidden composer state.
- In `SUBMITTED`, repeated Enter must not send a duplicate response.
- The transition from `CHOOSING` to `SUBMITTED` must be synchronous and keyed by
  request id.

### 4.3 Keyboard Behavior

Normal composer:

- Enter sends the current message.
- Shift+Enter inserts a newline.
- Up/Down navigates input history when the input is empty or already browsing
  history.

Choice mode:

- Up selects previous option.
- Down selects next option.
- Enter submits current option.
- Any other printable input is ignored.

OpenCode basis:

- Official keybind defaults use Return to submit, Shift+Return/Ctrl+Return/etc.
  for newline, Up/Down for history, and Up/Down/Return for dialog selection.

XBotv2 divergence:

- OpenCode also supports PageUp/PageDown message scrolling. XBotv2 should not
  introduce PageUp/PageDown as the primary scroll affordance because the current
  requirement asks for mouse wheel only.

### 4.4 Permission, Sandbox, and Ask-User Choices

Target:

- Permission/sandbox/ask-user requests render inline in the event stream.
- The visible row contains the prompt and selectable options.
- No Textual `Button` widgets for these options; they create focus ambiguity.
- Choices must be display rows controlled by the TUI mode controller.

OpenCode basis:

- OpenCode permission config uses `allow`, `ask`, `deny`.
- OpenCode approval UI has `once`, `always`, `reject`.
- OpenCode keybinds have generic dialog selection commands.

XBotv2 semantics:

- Permission persistence scopes already discussed for XBotv2 are:
  - non-persistent one-shot decision,
  - session-level decision,
  - always/personality-level decision.
- UI wording must match runtime behavior. Do not label something `always` if it
  only lasts for the current session.

### 4.5 Event Stream Rendering

Target row types:

- `user`: timestamp, "You", body.
- `assistant`: timestamp, agent name, body.
- `activity`: spinner/working state, elapsed time, per-turn usage.
- `tool_call`: tool name, compact args.
- `tool_result`: status and compact result preview, with cached/truncated
  indicator if applicable.
- `permission_request`: reason and choices.
- `ask_user`: question and choices/free-answer indication.
- `local_ack`: selected/submitted action, exactly once per request id.
- `usage`: reflected in status bar and activity row; usually not a standalone
  noisy row unless useful.
- `error/client_message`: inline notice.

Current defect evidence from real output:

- Chinese text appears as mojibake.
- Blank assistant blocks appear.
- `approval queued` appears twice.
- User still reports messages are swallowed.

Design requirements:

- Empty or whitespace-only assistant messages must not render as blank agent
  rows.
- Non-empty user/assistant messages must render exactly once.
- Tool calls in blank assistant messages must still render.
- Local acknowledgement rows must be keyed by request id to prevent duplicates.
- Message bodies must render as plain text, not Rich markup.

## 5. Verification Requirements

Do not claim TUI completion from unit tests alone. Required evidence:

1. Source-aligned spec:
   - This document cites OpenCode docs/repo and separates OpenCode behavior from
     XBotv2-specific decisions.

2. State tests:
   - Whitespace-only assistant content is ignored.
   - Tool calls from such assistant events are preserved.
   - Permission/ask-user pending state is preserved until acknowledgement.

3. Interaction tests:
   - Choice mode hides and disables composer.
   - Printable keys in choice mode do not mutate composer text.
   - Repeated Enter for the same request id emits one response and one local
     acknowledgement.

4. Replay tests:
   - Feed a recorded or synthetic LM Studio-style sequence:
     `user -> assistant tool_call with blank content -> permission_request ->
     permission_response_recorded -> tool_result -> final assistant`.
   - Export a Textual frame.
   - Assert every non-empty user and assistant message is visible.
   - Assert no blank agent block exists.
   - Assert no duplicate local acknowledgement exists.

5. Manual terminal tests:
   - Type Chinese directly in the real TUI, not via `load_text`.
   - Verify exact Chinese text in visible `You` row.
   - Verify exact Chinese text in `XBOTV2_TUI_TRACE` records for `tui.submit`
     and `protocol.send`.
   - Use mouse wheel to scroll.
   - Check whether native text selection works with current Textual mouse mode;
     if not, document the tradeoff and expose a config later.

6. Diagnostic trace:
   - Set `XBOTV2_TUI_TRACE=/tmp/xbotv2-tui-trace.jsonl` before launching the
     TUI.
   - The trace must record `tui.submit`, `protocol.send`, and `protocol.recv`
     events as UTF-8 JSONL.
   - For Chinese input debugging, compare:
     - `tui.submit.payload.text`
     - `protocol.send.payload.frame.payload.content` for `user.message`
     - `protocol.recv.payload.frame.payload.content` for server responses.
   - If `tui.submit` is already mojibake, the problem is in terminal/Textual
     input decoding. If `tui.submit` is correct but `protocol.send` is not, the
     problem is in client serialization. If both are correct but display is
     wrong, the problem is rendering.

## 6. Current XBotv2 State Against Requirements

| Requirement | Current status |
| --- | --- |
| Source-backed OpenCode analysis | Now documented here. |
| Single event stream | Partial; render paths are still split. |
| No right panel | Appears aligned in Textual code, but needs rendered-frame proof. |
| No multiple focus regions | Partial; transcript can be non-focusable, but real TUI behavior must be tested. |
| Choices inline, not buttons | Partial in current worktree; not yet accepted because duplicate ack occurred. |
| Input hidden during choice | Partial in current worktree; needs real rendered-frame proof. |
| Chinese input | Failing in real TUI; UTF-8 trace hooks now exist to locate the broken layer. |
| No swallowed messages | Failing or unproven; user reports failure. |
| No blank assistant blocks | Failing in real output; fix needed. |
| Usage display | Partial; status bar shows usage if usage frame arrives. |
| Mouse wheel scroll | Unverified manually. |
| Copy/select behavior | Unverified; OpenCode has explicit mouse config and selection code, XBotv2 does not. |

## 7. Implementation Plan After This Report

1. Revert or rework any previous TUI code that conflicts with this document.
2. Add protocol replay/debug capture before further visual claims.
3. Collapse visible rendering into one ordered render log.
4. Implement explicit UI mode controller.
5. Implement request-id-keyed interaction controller.
6. Filter blank assistant rows while preserving tool calls.
7. Add replay/SVG tests for the LM Studio tool-permission-final-answer path.
8. Diagnose Chinese input using `XBOTV2_TUI_TRACE` real terminal capture.
9. Run manual TUI verification and record the results in this document or a
   follow-up verification log.

## 8. Commit Hygiene

Only TUI-related files should be committed for this work:

- `XBotv2/docsv2/tui_opencode_requirements.md`
- `XBotv2/xbotv2/tui/*`
- focused TUI/protocol tests

Do not include unrelated changes in:

- root `main.py`
- `XBotv2/data/personalities/default/personality.yaml`

unless they are explicitly reviewed and required for the TUI task.
