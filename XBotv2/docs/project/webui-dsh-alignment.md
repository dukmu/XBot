# Aligning the WebUI (and TUI browsing) with DeepSeek Harness

The WebUI is not a re-design. It is **ported from DeepSeek Harness and then
adapted** to XBot's client state, because dsh's interface is a designed system
(60 scenario snapshots, 33 UI packages, a token layer with Figma-specified
geometry) while XBot's is not.

Source of truth: `output/deepseek-harness` (`@deepseek-ai/dsh-root`
0.1.0-rc.7, MIT) — `packages/client/ui-*` for components, `apps/web/tests/snapshots/*`
for the expected interface (aria snapshots per scenario).

### P2 subagent sessions (structure migrated, interactivity blocked)

Ported: the hierarchy breadcrumb (`nav "Session hierarchy"` → parent button /
`/` / current child), and `src/components/SubagentSessions.tsx` — a real tree
(`role="tree"`) built from `ThreadSummary.parent_thread_id`, showing each
session's state and token use, nesting descendants, collapsing a branch, and
rendering as a `N subagents` count button in the main transcript (dsh's
placement) while a viewed thread opens it.  It replaces the flat
`ThreadActivityPanel`, which is deleted.

**Not portable yet:** dsh's child sessions take typed input and can be
interrupted from the same composer.  XBot's server refuses that outright —
`session/protocol.py` answers "Subagent threads are read-only; switch to the
main thread to chat." — so the read-only banner stays until the API grows a
client-facing channel into a subagent thread.  Faking an enabled composer there
would be a lie about what the server does.

### P3 scenarios: goal bar

`src/components/GoalBar.tsx` ports dsh's `ui-goal` banner: the phase name
(`Ongoing Goal`, `Goal paused`, `Goal achieved`, …) beside the objective, its
round progress and stats, with `Pause goal` / `Edit goal` / `Clear goal`
actions.  It is the **first** consumer of `status_slots` in the WebUI, which
until now received the slots and rendered none of them.

Two adaptations worth naming:

- The objective had no slot, so the goal plugin now publishes
  `goal_objective` (short form) beside `goal` / `goal_round` / `goal_reason` /
  `goal_stats`; a status slot is the only way a client can read it without a
  command round trip.
- dsh's `Pause goal` has no XBot command.  Pausing in XBot *is* interrupting the
  running turn (the plugin parks the goal as `paused` when a turn is
  interrupted), so the button calls the session interrupt; `Edit goal` seeds the
  composer with `/goal ` and `Clear goal` runs `/goal clear`.

### P3 scenarios: to-do row

`ToolCall`'s to-do projection now matches dsh's row: the title is
`Update to-do list` (whatever XBot names the tool), the summary reads
`1/4 completed · <active item> +N` (the `+N` counts the *other* in-progress
items), and expanding shows a state tally (`1 completed · 2 in progress ·
1 pending`) above one `"<status> <subject>"` line per item.  A to-do item's id
moved to the row `title`, as the ported line has no room for it.

### P3 scenarios: output-limit notice

dsh's `max-tokens-notice` snapshot is `dot=warning / title=Output token limit
reached / hint=…Send "continue" to resume`.  XBot had no such signal: the
provider reports why generation stopped (Anthropic `max_tokens`, OpenAI
`length`) and the engine stores it in the assistant message's
`response_metadata`, but neither the event nor the protocol model carried it.

The chain is now complete, with the ported wording in
`MessageItem.tsx` (`OUTPUT_LIMIT_TITLE` / `OUTPUT_LIMIT_HINT`):

- `AgentLoopEngine` puts `response_metadata["stop_reason"]` on the
  `assistant_message` event;
- `AssistantMessageData` (the typed protocol model) declares the field, since it
  forbids extras;
- the Web reducer keeps it on the message entry (`MessageEntry.stopReason`) and
  `MessageItem` renders the warning below an assistant reply whose reason is
  `length` / `max_tokens` / `max_output_tokens` (never on a user message, never
  for `end_turn`).

### P3 scenarios: queue actions and steering

The backend half of these two scenarios was already complete and is covered by
the core suite (`test_agentloop_inbox.py::test_pending_input_mutations_replace_the_authoritative_snapshot`):
`GET/PATCH /sessions/{s}/threads/{t}/queue[/{message_id}]`, `MessageRequest.delivery`
(`queue` vs `steer`), and the `queue_updated` / `input_accepted` / `input_claimed`
/ `input_consumed` session events.  What was missing was the ported presentation:

- **Queue dock.** `QueueDock` now uses the scenario's strings — the count header
  is `N queued messages` (`QUEUE_COUNT`), and the row actions, the
  `Contains non-text content; editing is not supported yet` hint on an
  attachment-only row, and the `Steering is available only while the agent is
  running` hint on a stopped turn are named constants beside the component.
  Per-row delivery-phase text and the separate steering strip are gone: dsh
  renders neither, and the transcript row already carries the phase
  (`MessageEntry.deliveryState`).  The duplicated `deliveryStates` map in the
  runtime reducer went with them; the phases are projected once, onto the entry.
- **Steering rows.** A message the running turn has taken for its next step
  (`target: "next-step"`) leaves the dock and renders in the chat column as the
  user row it will become, after the transcript, marked `data-pending-steering`
  — dsh's `PendingSteeringBubble` projection.  `Timeline` takes the items and
  projects each onto a message entry (memoized, so the message memo and its
  Markdown cache survive).
- **Whole-queue gesture.** While a turn runs and the draft is empty, the
  composer's placeholder is `Cmd/Ctrl+Enter steers all queued messages`
  (`STEER_QUEUE_PLACEHOLDER`) and that chord steers every still-queued message
  in FIFO order instead of submitting the empty draft
  (`useXBot.steerAllPending`, dsh's `steerQueue`).  A row the host already
  steered or claimed converges silently (the 404 the queue routes return), so
  repeated chords are a no-op; a genuine failure surfaces one notice.  Unlike
  dsh's gate, XBot also requires a non-empty queue, so the hint never advertises
  a no-op.

Not ported: dsh's hover `Tooltip` wrapper around the row actions (XBot's rows
use the same text as a native `title`), which is why the scenario's `tooltip`
nodes have no XBot counterpart.

### P3 scenarios: math rendering

dsh's `math-rendering` snapshot (`.katex` ×6, `.katex-display` ×2,
`.katex-error` 0) is the checklist; its fixture exercise five shapes: inline
`$…$`, inline `\(…\)`, display `\[…\]`, a same-line `$$…$$` block with `\tag`,
and math inside table cells.

XBot rendered markdown through `react-markdown`, which had no TeX at all. The
port is two pieces:

- `src/markdown/mathCompatibility.ts` — dsh's own micromark syntax extension
  (`packages/client/ui-primitives/src/markdown/mathCompatibility.ts`), vendored
  verbatim under a header naming that path. It is what makes the backslash
  delimiters `\(…\)` / `\[…\]` and the same-line `$$…$$` block parse, reusing
  `micromark-extension-math`'s token vocabulary; `remark-math` alone covers only
  dollar math, and treats same-line `$$…$$` as inline.
- `src/markdown/remarkDshGrammar.ts` — the only adaptation: it registers the
  vendored grammar extensions on the processor `react-markdown` builds, whose
  `remark-parse` reads syntax extensions from the processor's data bag, where
  dsh's pipeline hands them to `fromMarkdown` directly. The grammar and the
  emitted `math`/`inlineMath` nodes are upstream's; the module also carries the
  CJK extension below, in dsh's registration order.

`MessageItem`'s plugin list is `[remarkGfm, remarkDshGrammar,
remarkMath]`, matching the extension order of dsh's parser, and `rehype-katex`
plus `katex/dist/katex.min.css` (KaTeX's own stylesheet, as the ported renderer
loads it) renders the nodes. `src/components/markdown-math.test.tsx` renders the
scenario's fixture delimiter for delimiter and asserts the six roots, the two
display blocks, six MathML arms and no error span; dropping the vendored
extension leaves two roots, so the test fails without the port.

The micromark packages the vendored module imports are declared as direct
dependencies of `XBotv2/web` rather than left transitive.

### P3 scenarios: CJK strong emphasis

dsh's `markdown-cjk-strong` snapshot is eight blocks where `**` closes after
punctuation while CJK prose continues with no whitespace (`**注意：**内容`).  Stock
CommonMark leaves those asterisks literal, so dsh registers its own asterisk
attention construct — `cjkFriendlyStrong.ts`, vendored verbatim as
`src/markdown/cjkFriendlyStrong.ts` and registered by `remarkDshGrammar` before
the math delimiters, as in dsh's parser.

`src/components/markdown-cjk-strong.test.tsx` builds the scenario's fixture the
way dsh builds it (one block per case) and asserts the eight `<strong>` runs and
their eight paragraphs; dropping the grammar extension fails both tests.

### P3 scenarios: markdown image policy

dsh's `markdown-images` snapshot renders a remote image as `img "<alt>"` and a
*local* path as its alt text: `render.tsx` only fetches an absolute `http(s)`
source (`remoteImageUrl(sanitizeUrl(url))`), which is why the scenario's
`./local-image.png` never becomes an image request.

Both helpers are vendored verbatim as `src/markdown/mediaPolicy.ts`, and
`src/markdown/MarkdownImage.tsx` is the ported element: remote sources render the
`<img loading="lazy" decoding="async" referrerPolicy="no-referrer">` with dsh's
own `.image`/`.imageAlt` rules (vendored into `MarkdownImage.module.css`),
anything else renders the alt text in the `.imageAlt` span.  `MessageItem` passes
it as react-markdown's `img` component, from a stable module-level map so the
processor is not rebuilt per render.

`src/components/markdown-image-policy.test.tsx` covers the scenario's two images
plus `data:` and `file:` sources; bypassing the policy makes all three tests
fail.

### P3 scenarios: skill surfaces

XBot's skills subsystem already existed (`XBotv2/skills/`); the transcript had no
skill presentation at all, so a loaded skill and a slash-invoked skill both fell
through to generic rows.

- **Skill tool row.** dsh renders one `skill` tool as a compact `Skill <name>`
  row that opens a labelled instructions card plus an `Inspect` button
  (`ui-skill/SkillRow.tsx`).  XBot registers one tool *per skill*, named after
  the skill and taking no arguments, so the row is the same shape with the skill
  name in the summary slot: `ToolCall`'s `skill` prop switches the title to
  `Skill`, the leading glyph, the default-open disclosure, the
  `aria-label="Instructions"` card (dsh's own geometry and `--dsw-*` tokens in
  `global.css`) and `Inspect`, which selects the row into the details column.
- **Identifying a skill call.** The only honest signal is the tool catalog: skill
  tools are registered under the `skills:<scope>` namespace, and
  `GET /sessions/{s}/threads/{t}/tools` exposes it.  `api.listTools` plus
  `state/skillTools.ts` (`skillToolNames`, namespace prefix, both the
  model-facing and the registered name) feed `useXBot.skillTools`, which `App`
  passes to `Timeline` → `ConversationNode` → `ToolCall`.
- **Slash-invoked skill.** The skills plugin answers
  `BEFORE_USER_MESSAGE_ACCEPT` by *replacing* the accepted input with its
  `<skill_invocation name="…">` prompt container, so the transcript used to show
  that envelope verbatim in a user bubble.  `runtime.ts` now parses the
  container (`skillInvocationSource`) and projects it as the runtime entry dsh
  shows — `Context injection <skill>` — on both the live event path and history
  replay.
- **Injected context copy.** That row's title was XBot's own ("Injected
  context"); it is now dsh's `Context injection`, followed by the separator,
  the producer and the detail, so the accessible name matches the snapshots
  (`Context injection @deepseek-ai/dsh-system-prompt`, `… goal`,
  `… user-invoke-demo`).

Verified by `state/skillTools.test.ts`, the new `ToolCall` skill-row cases, the
reducer/history case in `state/runtime.test.ts`, and the updated
`ContextInjectionRow.test.tsx`; disabling the skill branch or the container
parser fails those tests.

Two parts of these scenarios stay unported, both because the data does not exist:

- **`Context injection skill-catalog`.** dsh injects a skill catalog into the
  session context; XBot's skills plugin only contributes tool descriptions and a
  metadata budget for tool *selection* (`_on_before_tool_schema`), so there is no
  catalog message to render.
- **The skill group in the command menu.** dsh's trigger menu groups skills and
  badges them (`policy-user-only user-only · Available only to user invocation`).
  XBot's command catalog carries no skill marker or availability — skill commands
  are ordinary `kind="prompt"` entries — so a menu group would have to be
  invented from the command name alone.  A `skill` marker (or an availability
  field) on `CommandDescription` is the missing piece.

### P3 scenarios: background job list

dsh's `background-job-list` snapshot is one `list "Background jobs"` whose items
read `kind label status duration`, with the trigger labelled by the live count
(`1 background job running`, `2 background jobs`).  `JobDock` now renders exactly
that: the ported count label, live rows first in start order and settled rows
newest-first (`orderedJobs`), a duration that ticks while the list is open
(`formatJobDuration`, at most two adjacent units) and the failure's first line in
the status slot (`jobDetail`).

Two XBot affordances stay, because removing them would drop capability rather
than change presentation: a per-row stop button on live jobs and a disclosure
button that opens the job's output beneath its row.  Both live inside the `<li>`,
so a closed row still reads `kind label status duration`.

The reducer also stopped discarding settled jobs in the `jobs` snapshot: keeping
them only when they arrived through `job_updated` made the row's survival depend
on the transport path, while the ported list deliberately shows settled rows.

Covered by `src/components/JobDock.test.tsx` (trigger label, list/row shape,
settled row without a stop control, output disclosure and stop wiring, duration
and ordering projections) and a reducer case in `state/runtime.test.ts`.

Differences that are data, not presentation:

- The scenario's rows read `bash`; XBot's job kind is `shell`, and the row shows
  the kind it actually has.
- The settled row in the snapshot reads `signal: SIGTERM`.  XBot's `JobSnapshot`
  has no `detail` field (and stops shell jobs with `SIGKILL`), so the row shows
  the status word `stopped`.  A `detail` field on the job snapshot is the missing
  piece.

### P3 scenarios: approval and question cards

dsh renders a pending permission and a pending question *in the transcript*, not
over it: `access-confirmation` / `approval-composer` show a
`group "Approval details"` with its `Reject` / `Allow once` decisions, and
`question-composer` shows a `region` named by the question, an option group, a
`Type your answer` box and `Submit`.  XBot rendered both as a modal
(`InteractionDialog`), so the transcript was hidden exactly while the turn needed
an answer.

`src/components/PendingInteraction.tsx` is the ported card, and the modal is
gone:

- **Approval** — `role="group"` labelled `Approval details` (dsh's string) around
  the tool and its arguments plus the escalation reason, then `Reject` /
  `Allow once` and XBot's extra `Allow session`, which maps to the scope its
  interaction service already accepts.
- **Question** — `role="region"` labelled by the question, an `<h2>` with the
  question, an unnamed option group of radios carrying each option's label and
  description, the `Type your answer` textarea, and `Submit` (disabled until
  something is chosen or typed).
- **Placement** — `Timeline` takes the card as `interaction` and renders it after
  the transcript rows, so `App` no longer mounts a dialog for it.

Covered by `src/components/PendingInteraction.test.tsx` (approval group and
decisions, session scope, question region with option submit, typed answer) and a
`Timeline` case asserting the card lands after the transcript.  The `App` prop
wiring itself has no automated test; the build and the live page were checked by
hand.

What the ported card cannot show, because XBot's request has no field for it:
`header`, `multi_select` (XBot's `ask_user` asks one single-choice question with
at least two options), question ids, several questions per request with
`Previous` / `Next` and `1 / M`, `Collapse the question card`,
`Dismiss all questions`, and `Skip this question` (the interaction service
answers or cancels; there is no "skipped" answer).  The sidebar's
`Waiting for answer` / `Waiting for approval` badge is the same
`SessionSummary` gap noted below.

### P3 scenarios: search card

dsh's `search-card` snapshot is a key=value capture of the grep card model:
a banner summary, matches grouped per file with a per-file count, `<line>:
<text>` rows, a head/tail height cap with `… N more rows`, and a recovery line
for a capped result.  XBot rendered every search as the tool's raw JSON inside a
generic result block.

`src/components/SearchCard.tsx` is the ported card, over XBot's own search
result (`searchCard` maps `data.matches` to the grouped shape and `data.files` to
the flat path list, so both search modes land on one of dsh's two card shapes):

- banner summary (`N matches · M files` / `N paths`), with a copy control that
  writes the same plain text dsh's does (`path`, then `line: text` rows);
- per-file header buttons with the match count and a collapse toggle;
- the ported head/tail cap (`headTailCap`: `ceil(maxLines / 2)` head rows, the
  rest tail, the middle behind `… N more rows`), at the chat cap of 8 rows —
  `CHAT_SEARCH_MAX_LINES`, as in dsh's chat model;
- a `truncated` note when the search stopped at its limit.

The card serves both render sites, as dsh's model does: the chat row and the
details column (which keeps the fuller 16-row cap, the primitive's own default).

Covered by `src/components/SearchCard.test.tsx` (summary and grouping,
per-file collapse, head/tail cap arithmetic and expansion, path list and capped
note, copy text, the result-shape derivation) and by rows in
`ToolCall.test.tsx` and `DetailsPanel.test.tsx`.

Differences that are data or copy, not structure:

- dsh's summary folds in the pre-cap total (`显示 9 / 共 42 处匹配 · 3 个文件`).
  XBot's search reports `returned_matches` and a `truncated` flag but no total
  count, so a capped card reads `first 9 matches · 3 files`.  A total on the
  search result is the missing piece.
- dsh's capped result also carries a recovery locator (`Full … stored at …`) in
  the raw result text, which the card surfaces because it has replaced that text.
  XBot's search has no spill locator, so the card says only that more matches
  exist.
- The ported primitive hard-codes Chinese copy (`显示`, `复制`, `无结果`,
  `… 其余 N 行`); XBot renders the English equivalents, since its UI is English.

### P3 scenarios: agent preset selection

dsh's `agent-preset-selection` snapshots are the composer's preset menu: a
`menu` whose `menuitem`s carry the preset name **and the description the catalog
gives it** (`Standard mode Full coding agent with file editing, shell, file and
web search, …`), with a check icon on the running one, plus the hero's
`button "Standard mode"` before a session exists.

XBot selected the mode from a `<select>` that showed names only, so a definition's
description was never visible anywhere in the WebUI.  `AgentPresetMenu` (in
`ComposerRuntimeControls.tsx`) is the ported control: the trigger keeps the
`Standard mode, current: <name>` accessible name, `aria-haspopup`/`aria-expanded`
and the select chrome, and the menu lists every non-subagent definition with its
name and description, marking the running one.  Subagent definitions stay
unselectable, as before, and before a session exists the trigger is the disabled
`Standard mode` button the hero snapshot shows.

`agent-preset-authoring` (copy dialog, created / damaged preset sections) is not
ported: XBot's definitions come from configuration and there is no authoring
operation to write one, so the settings section would have nothing behind it.

### P3 scenarios: trajectory pane

Every dsh session carries `tab "Chat"` next to `tab "Trajectory"`, and
`navigation-panes/trajectory` describes the pane behind it: a
`toolbar "Trajectory toolbar"` with a `searchbox "Search trajectory"` and the
`Duration` / `Turns` / `Calls` controls, over a `region "Trajectory timeline"`
whose table lists one record per row with its turn / request label and its
timing (`Total 1,542 ms · TTFT 368 ms · Decoding 1,174 ms`).

XBot fetched the trajectory and projected it into the chat flow only, so the
durable records had no surface.  `src/components/TrajectoryPane.tsx` is the pane,
and `RuntimeHeader` now renders the ported `tablist`:

- **Records, not lifecycle.** `trajectoryRows` numbers turns from the
  trajectory's own `turn_started` events and requests per assistant message, and
  lists the conversation records (`USER` / `ASSISTANT` / `TOOL`, plus a
  compaction `surface_replace`); the lifecycle events themselves only drive that
  numbering, as the ported table lists records rather than events.
- **Toolbar.** `Duration` (`aria-label` `Use actual duration`) shows or hides the
  timing line, which `formatTiming` renders from XBot's own timing payload
  (`llm_ms` / `ttft_ms` / `decode_ms`); `Turns` collapses each turn to one summary
  row (`4 records · 2 requests`); `Calls` drops tool rows; the search box filters
  role, group label and content.
- **Selection and paging.** A tool record carries its `tool_call_id`, so picking
  it opens the details column for that call (the row is marked `aria-selected`),
  and the pane's `Older records` control runs the trajectory paging the chat flow
  already used (`trajectory_prepend` + `state.windowCursor`).

Covered by `src/components/TrajectoryPane.test.tsx` (toolbar and table shape,
selection payload, search filtering and the empty state, turn / call collapse,
duration toggling, turn and request numbering, timing formatting, records without
a turn).

Not ported, and why:

- **Proportional duration layout.** dsh's `Use actual duration` control changes
  the row geometry to the record's wall-clock length.  XBot has the timings (and
  renders them) but no layout model to port, so the control toggles the timing
  line instead of the geometry.
- **`SYSTEM` records.** dsh's table starts with `SYSTEM, Initial System Prompt`.
  XBot's trajectory holds user / assistant / tool records (the system prompt is
  assembled per request and is not a durable record), so there is no system row
  to show.

### P3 scenarios: workspace directory browser

dsh's `workspace-management/directory-browser` snapshot is the in-app picker: a
`Select Workspace Directory` heading, a breadcrumb whose last crumb names the
path the browser would open (`Home > browse-golden`) with a click-to-edit path
zone beside it, a Miller view (the level's folders, and the selected folder's
children in a second list), a `Show hidden files` toggle, and `Cancel` / `Open`.

XBot's picker was a different shape: a permanent path input, parent/home icon
buttons, a single `listbox` of folders and a `Select` button, and — a real layout
bug the live check surfaced — its dialog was `display: flex` without a direction,
so every child (header, list, footer) was laid out in a **row**.

`DirectoryBrowser.tsx` is now the ported picker:

- **Breadcrumb** — `directoryCrumbs` builds the chain from the target path (the
  selection when there is one, else the listed level), starting at `Home` inside
  the home subtree and at the filesystem root outside it; each crumb navigates,
  and the pencil button is dsh's `Edit path` (the editor opens seeded with a
  trailing separator and submits through the same loader).
- **Two panes** — clicking a level's folder selects it and lists its children
  beside it; clicking a child descends, which is how the ported view shifts one
  level deeper. `Open` adopts the selection, else the listed level, and the
  footer shows which.
- **Hidden entries** — the footer's `Show hidden files` / `Hide hidden files`
  toggle (`aria-pressed`) reveals the host-flagged entries, as dsh's does.
- **Layout** — the dialog is a flex column again, so header, panes and footer
  stack as the ported geometry expects.

Covered by `src/components/DirectoryBrowser.test.tsx` (navigation and selection
through the two panes, descending from the child list, the `Edit path` zone and
its seeded separator, the hidden toggle and `Cancel`, and the crumb derivation on
both branches).  The dialog's rendered structure and two-pane behaviour were also
checked on the live page (aria snapshot + screenshot), which is how the flex
layout bug was found.

`New folder` remains unported; see the backend gaps below.

### P3 scenarios: declared reasoning effort

dsh's `declared-reasoning` snapshot is a `menu "模型与推理等级"` of
`menuitemradio`s — `Default` (checked), then the levels the running model
declares.  XBot chose the level from a `<select>`, so the levels and the model's
own default were only visible as native options.

`EffortMenu` (in `ComposerRuntimeControls.tsx`) is the ported control: the
trigger keeps the `Reasoning effort, current: <level>` name with
`aria-haspopup`/`aria-expanded`, the menu lists `Default` beside the declared
levels as `menuitemradio`s with `aria-checked`, and choosing `Default` sends the
empty level (the model's own), not a level name.

Covered by `ComposerRuntimeControls.test.tsx` (the level list with the explicit
default, the selected state, and both transitions — a level, then back to
default).

### P3 scenarios: frame drag handles

`details-session-lifecycle/handles` probes the frame's drag handles: an 8px hit
strip on the column border, `cursor: col-resize`, and **no pill on the sidebar
handle** — dsh's own rule is "details adds a visible 12x32 pill at vertical
center; sidebar keeps only the hit strip".

XBot's frame already had a sidebar handle with pointer capture, clamping and a
persisted width, but it drew a hover pill on the sidebar too, which is the one
thing the ported frame does not do.  `DshAppFrame` now matches: the sidebar keeps
the bare hit strip, and the **details column has its own handle** with the ported
pill (`data-side="details"`, 12×32, revealed on hover or while dragging), dragging
the column between 280 and 560 px and remembering the width like the sidebar's.

Covered by `DshAppFrame.test.tsx` (handle sides, sidebar drag and clamping with
the persisted width, details drag widening the column).  jsdom has no pointer
capture or `PointerEvent`, so those tests install the two capture methods and
assign the pointer fields onto a plain event — stated in the test file rather
than hidden.

### P3 scenarios: session rail groups and the turn footer

Two smaller ports from the same snapshots:

- **`Ungrouped` sessions.** `message-actions/fork` shows the rail after a fork:
  `tree "Sessions"` → `treeitem "Ungrouped" [expanded]` → the sessions.
  XBot rendered those sessions under a bare `Other sessions` label, and only when
  at least one workspace existed.  They now sit under a named, collapsible
  `Ungrouped` node (`UNGROUPED_LABEL`) with the workspace groups' own toggle
  markup and a count, and the node appears whenever such sessions exist.
- **Turn footer.** `stats-paged-history` / `turn-tail-actions` end a settled
  reply with `Ran for <duration> TTFT <duration> <throughput> tok/s`.
  `MessageEntry` now carries the durable record's `timing` (threaded through
  `historyEntries`, so both the trajectory projection and a history replay have
  it) and `MessageItem` renders `Ran for 1.5 s · TTFT 368 ms`
  (`formatTurnDuration`: `850 ms` / `1.5 s`) for a settled assistant reply that
  has one.

Covered by `SessionSidebar.test.tsx` (the node's name, expanded state, its
sessions and their collapse) and `MessageItem.test.tsx` (the footer's text, its
absence without timing and on a user row, and the duration formatter).

Two parts of dsh's footer have no XBot source and are therefore absent rather
than invented: the **clock** before `Ran for` (XBot's `HistoryItem` has no
timestamp) and the **throughput** in `tok/s` (usage is recorded per session, not
per message).

### P3 scenarios: composer draft scrolling

`composer-draft-scroll/geometry` describes dsh's draft as **14 lines, two text
layers, one scrollport**: a hidden mirror in normal flow sets the full draft
height, the textarea rides it absolutely, and `max-height` + `overflow-y: auto`
live on the single scrolling box — so the textarea "holds no scroll offset of its
own" and the glyphs and the caret can never drift apart.

XBot grew the textarea from JavaScript between a 46px floor and a 180px ceiling:
past the ceiling the **textarea scrolled itself**, which is the arrangement the
scenario exists to rule out.

The composer now has the ported stack: `.composer-draft-scroll` (the only
scroller, capped at 14 lines of XBot's own metrics) wrapping
`.composer-draft-grow` → the hidden `[data-input-mirror]` carrying `${draft}\n`
plus the absolutely positioned textarea (`height: 100%`, `overflow: hidden`, no
resize).  The old height effect is gone, and `revealCaret` is the ported caret
reveal — it measures the caret with a Range over the mirror and scrolls the
scrollport the minimum, including the trailing-newline case the engines disagree
about, so a pasted block still lands on its last line.

Covered by `ComposerSteerQueue.test.tsx` (the textarea sits inside the scrollport
beside its `aria-hidden` mirror, and the mirror stays in step with the draft).
The CSS (the cap on the scrollport, the shared metrics of both layers, and the
mobile override) ships in the built bundle.

**Not verified live**: the composer's enabled state needs a session, and creating
one on this machine fails with `session_open_failed: Environment variable
MINIMAX_API_TOKEN is not set` — the same reason no scenario in this port has a
live-session screenshot.  The draft composer's locked rendering was checked on
the live page after the change (screenshot
`.dsh-browser/screenshots/composer-draft-after-scrollport.png`).

### P3 scenarios: scrollbar skin, column overflow and tab geometry

Three geometry scenarios, one of which found a real defect:

- **`sidebar-scrollbar`.** dsh's rail list keeps `scrollbar-gutter: stable` (the
  8px reserved band), draws the bar from the vendored sheet (8px, transparent
  track, thumb colour from the `--dsh-scrollbar-thumb{,-hover}` indirection) and
  picks the elevated **l2** pair by rebinding that indirection, with the
  pointer-outside state rebinding it to `transparent` so the reveal never
  reflows a row.  XBot's list instead set `scrollbar-width: thin` and
  `scrollbar-color: transparent transparent` **unconditionally** — which, by the
  vendored sheet's own contract, makes Chromium drop every
  `::-webkit-scrollbar*` rule for that element, so the thumb-hover token could
  never render, and it hard-coded a palette variable instead of the token pair.
  The list now takes the ported skin: `scrollbar-gutter: stable` on the scroller,
  no standard properties, and `.session-sidebar` rebinding the indirection pair
  (`transparent` at rest, the l2 thumb under `:hover`, the hover token for the
  thumb itself).
- **`conversation-column-overflow`.** The scenario wants the conversation column
  `overflow-x: hidden` at every width with no horizontal scroll.  XBot's
  `.conversation-scroll` already declares `overflow-x: hidden` with
  `overflow-y: auto`, so this one is satisfied by the existing rule rather than
  by new code.
- **`composer-tab-geometry`.** dsh reserves the chat scroller's gutter
  (`scrollbar-gutter: stable`, 8px band) and leaves the trajectory pane at
  `auto`, because its overlay composer sits *inside* the scrolling column and a
  appearing bar would move the card.  XBot's `.conversation-scroll` already
  carries the same reservation, the trajectory pane deliberately declares none,
  and XBot's composer is a flex **sibling** below whichever surface is active —
  so the card's edges and width cannot depend on either scroller.

Verified by the shipped CSS (the reservation and the rebind are in the built
bundle) and by reading the existing rules for the other two; none of the three
has a live measurement, because they need a session or an overflowing rail list.

### P3 scenarios: access confirmation

`access-confirmation` is the Full-access gate: a `dialog` titled by the confirm
copy, a warning paragraph, an **acknowledgement checkbox**
(`I understand the risks and want to continue`), `Cancel`, and an
`Enable Full access` button that stays `[disabled]` until the box is checked —
dsh's `RiskConfirmation` primitive, which exists precisely so a sensitive action
cannot be armed by one click.

XBot had the copy but not the gate: Full access was confirmed by a section inside
the access-mode menu with an always-enabled button and no acknowledgement at all.
`AccessModeButton` now renders the ported dialog (own backdrop, Escape and
mask dismissal, `Close`, the warning block, the auto-focused checkbox) and only
applies the preset once the acknowledgement is checked; the menu closes under it.

Covered by `AccessModeButton.test.tsx` (the dialog's title and copy, the disabled
primary action, the acknowledgement enabling it, the applied preset, and the
cancel path leaving the policy untouched).  **Falsified**: removing the
acknowledgement from the disabled condition fails that test.

## What is already aligned

| Area | dsh | XBot |
|---|---|---|
| Sidebar grouping | `tree "Sessions"` → `workspace` node → sessions, plus an `Ungrouped` node | `SessionSidebar` groups by `workspaces[].session_ids`, with the `Ungrouped` node for the rest |
| Conversation | `tab "Chat"` | the message flow (`Timeline`) |
| Trajectory | `tab "Trajectory"`: toolbar (`Duration` / `Turns` / `Calls`, trajectory search) over a SYSTEM / USER / ASSISTANT / TOOL record table | `TrajectoryPane` behind the header's `Chat` / `Trajectory` tabs, over `state.trajectory` |
| Message chrome | copy / branch actions (feedback pending its backend) | `MessageIconActions` (copy, regenerate, fork) |
| Jobs, todos, plugin config | `ui-jobs`, `ui-todo`, settings plugins | `JobDock`, `TodoDock`, `PluginConfigPanel` |

## What is missing (the work)

1. **Cold start.** dsh boots *into a session*: `New Session` [selected], the live
   composer, `Choose workspace` unlocking it, mode / access mode / model /
   `Commands` controls, and a `Details` panel with a hint. XBot shows an
   `empty-workbench` placeholder ("No session selected") and gives up the rest.
2. **Subagent sessions.** dsh navigates them as sessions inside one chat frame:
   a `Session hierarchy` breadcrumb (`parent / child`), a `Subagent sessions`
   tree (state, token count, `continuable` vs `one-shot`, nested descendants),
   the same chat components, and a composer that can message/interrupt the child.
   XBot replaces the main transcript with a read-only panel and disables input.
3. **Visual system.** dsh has one token layer (`--dsw-static-*` palette →
   `--dsw-alias-*` semantics → component CSS) plus scrollbar, shiki code theme
   and gradient sheets, with per-component CSS modules quoting Figma geometry.
   XBot had a hand-copied subset (`styles/dsh.css`) beside its own palette
   (`styles/global.css`, 139 local variables and hard-coded colours), so the two
   disagreed component by component.
4. **Scenarios with no XBot equivalent** (from the snapshot list): details panel,
   goal bar and goal multi-turn actions, onboarding (two), search card,
   agent-preset authoring, workflow run, schedule-after, the settings chrome
   (general / models / plugins / presets with the models editor), the workspace
   `New folder` action, sidebar subagent activity.  (Math rendering, the max-tokens notice,
   queue actions / steering, CJK strong emphasis, the markdown image policy, the
   skill surfaces, the background job list, the inline approval / question cards,
   the search card, agent preset selection, the trajectory pane, the workspace
   directory browser, the declared effort menu, the frame drag handles, the
   `Ungrouped` rail node, the turn footer, the composer draft stack, the
   scrollbar skin and the access confirmation are done; see their sections
   above.)  Plan review and the
   session-feedback surfaces are not UI work at all — see the backend gaps below.

## Backend gaps (UI cannot be ported yet)

These dsh surfaces have no XBot capability behind them, so porting the UI would
mean inventing behaviour.  They are reported rather than built:

- **Session log / `export`.** Every dsh banner carries a `Session log` button and
  its command menu offers `export Download this Session log as a ZIP archive`.
  XBot's session API has no log or export operation at all
  (`XBotv2/session/protocol.py` lists open/list/get/fork/delete, threads,
  messages, trajectory, artifacts, history, queue, events, interactions,
  interrupt, close — nothing else), so there is nothing to display or download.
- **Session feedback.** dsh renders `Good response` / `Bad response` under a
  settled reply and has a `feedback` command plus its own protocol.  XBot's
  `MessageIconActions` has copy/regenerate/fork only, and no feedback event,
  operation or store exists.
- **Plan mode.** dsh's `plan-review` / `plan-active` chrome and the `plan`
  command have no XBot counterpart: no plan mode, no plan document, no review
  state in the runtime.
- **Subagent-session input.** The transcript of a subagent thread is readable,
  but `session/protocol.py` answers a send with "Subagent threads are read-only;
  switch to the main thread to chat", so the ported composer cannot be enabled
  there.
- **Sidebar subagent activity.** dsh's `sidebar-subagent-activity` row carries
  `1 subagent running` for a background session.  XBot's `SessionSummary` exposes
  `active_threads`, which counts every live thread of that session (the main one
  included) and `thread_count`, which counts all threads; neither distinguishes
  subagents, and the threads of a non-current session are not loaded in the
  client.  An `active_subagents` count on the session summary is the missing
  piece.
- **Job row detail.** See the background-job-list section: the wire job snapshot
  has no `detail`, so a stopped row cannot name the signal the way dsh's does.
- **Preset authoring.** dsh's settings has an `Agent 预设` section that copies an
  existing preset into a new one (and flags a damaged one).  XBot's agent
  definitions are configuration inputs with no write operation, so there is
  nothing for a copy or repair action to call.
- **New folder in the workspace picker.** dsh's directory browser has a
  `New folder` button; XBot's only filesystem HTTP operations are read-only
  listing (the `mkdir` capability exists inside the agent's tool path, not on the
  client API).
- **Message timestamps.** dsh's turn footer opens with the message's clock and
  reports throughput in `tok/s`; XBot's durable messages carry no timestamp and
  its usage is per session, so the footer shows only the durations the record
  holds (see the session-rail / turn-footer section).
- **Search totals.** XBot's search result reports the retained matches, not how
  many the search found, so a capped card cannot say "9 of 42" the way dsh's
  does; see the search-card section.
- **Question batches.** dsh's `ask_user_question` takes several questions with a
  header and a multi-select flag; XBot's `ask_user` takes one question, at least
  two options and no header, so the ported card's paging, dismiss-all and skip
  chrome has nothing to act on.  Question ids, a `header`, `multi_select` and a
  no-answer ("skipped") status are the missing pieces.

Skills are the opposite case: XBot *does* have a skills subsystem
(`XBotv2/skills/`), so the three skill scenarios are UI-only work and stay on the
list above.

## Porting rules

- **Vendor whole files, do not paraphrase.** Anything taken from dsh is copied
  byte-for-byte (styles) or as a whole component (tsx + its `.module.css`), with
  a header naming the dsh path; adaptation happens in XBot's own wrapper, not by
  editing the ported file.
- **One token layer.** `src/styles/dsh/` holds the vendored sheets verbatim
  (`base`, `design-platform`, `scrollbar`, `shiki`, `gradient-shadow-text`,
  imported by `main.tsx` before `global.css`/`dsh.css`). XBot's own sheets must
  consume `--dsw-*` instead of adding a parallel palette; removing the duplicate
  variables in `global.css` is part of the work, not done yet.
- **Copy the scenario, not the code path.** Each dsh snapshot is a checklist
  item; the acceptance test is XBot's own test for the same structure, not a
  copied dsh test (their harness is cordis/slot based).

### P1 cold start (in progress)

XBot now boots into a **draft session** instead of an `empty-workbench`
placeholder: `src/components/DraftSession.tsx` renders the hero and the locked
composer, with `Choose workspace` opening the existing new-session picker.

The **details column** is ported too: `DshAppFrame` gained the third grid track
dsh has (`detail`, hidden on mobile), `src/components/DetailsPanel.tsx` is the
column itself (head + `Close details`, verbatim empty-state hint, and the picked
tool's arguments/result/artifacts), `ToolCall` reports the row it belongs to
(`onSelect`, `selected`), and `App` keeps the selection and feeds it to the
column for both the main transcript and a read-only thread view.

The composer's bottom row is being filled too: `Commands` and the **access
mode** control are in (`src/components/AccessModeButton.tsx`).  XBot's
`SessionPolicy` (sandbox + permissions) had no WebUI surface at all; the control
now reads `effective_sandbox`, maps it to dsh's host-supplied presets
(`readonly` / `workspace-write` / `danger-full-access`), title-cases them the way
dsh does, and keeps Full access behind dsh's confirmation copy.  Applying a
preset PATCHes `/sessions/{id}/policy` (`useXBot.updateSessionPolicy`).

`Standard mode` and `Select model` are ported as well
(`src/components/ComposerRuntimeControls.tsx`): the agent/mode and
provider/model/effort selectors moved out of the header and into the composer,
where dsh keeps them, and they render locked in the draft session.  The header
keeps only the mobile menu's compact copies.

Phase P1 is therefore complete against the `lifecycle-chrome/hero` snapshot:
`Choose workspace`, `Standard mode`, the composer, `Commands`, `Access mode`,
`Select model`, `Send`, and the details column with its hint.

Verified by `src/components/DraftSession.test.tsx`, by the live page's aria
snapshot (`Choose workspace` + disabled `Message XBot` textbox), and by
screenshots (`.dsh-browser/screenshots/xbot-web-coldstart-{before,after}.png`,
taken against a local `xbot web` on 127.0.0.1:4173).

Current state: token layer vendored and guarded by
`src/styles/dsh-tokens.test.ts`; verified in the built bundle
(`XBotv2/web_dist/assets/*.css` contains `--dsw-static-blue-500`,
`--dsw-font-family`, `--shiki-foreground`).  Component porting has not started.

## TUI: `/session` browsing

The terminal had one flat list (`title  id  workspace` per row) while the Web
rail groups by workspace. `/session` now browses **workspace first, then that
workspace's sessions** whenever more than one workspace exists, using the same
`GET /workspaces` catalogue (`WorkspaceSnapshot`, exposed as
`XBotClient.list_workspaces` / `TerminalSession.list_workspaces`).  A single
workspace, or a server without the catalogue, keeps the previous flat list, so
`/session new` and `/session <id>` are unchanged.
