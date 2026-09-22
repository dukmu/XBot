# TUI and WebUI performance architecture

Goal: memory and per-update time stay bounded as a conversation grows. "Bounded"
means O(1) **with respect to conversation length** — work proportional to a fixed
window (a few hundred entries) is fine, work proportional to the whole history is
not.

> **The TUI half of this document is historical.** The TUI client was rewritten
> from scratch (`XBotv2/tui/`; contract and decisions in `tui-rewrite-spec.md`),
> and every module named in the TUI sections below — `tui/client.py`, `TuiState`,
> `TranscriptSurface`, `_MAX_STATE_*`, `tests/core/test_tui_client.py`,
> `tests/bench/test_tui_event_throughput.py` — was deleted with it. What is written
> about that implementation is kept as the record of what it did, not as a
> description of the current client. The current TUI's numbers, and the one TUI
> goal it does **not** meet, are in `tui-rewrite-spec.md` §5.16: the new client
> renders through a bounded window (`tui/view/plan.py` + `tui/view/transcript.py`)
> but **retains the whole history in memory** and has no eviction path, so the
> "bounded data" half of this goal is met on the Web and not on the TUI. The Web
> and protocol sections below are current.

## What was unbounded, and where it stands

| Client | State | Grows with | Status |
|---|---|---|---|
| Web | `RuntimeState.entries: TimelineEntry[]` | whole conversation | **fixed** — capped at `MAX_TIMELINE_ENTRIES = 240` by `boundTranscriptWindow` |
| Web | `RuntimeState.trajectory: TrajectoryItem[]` | whole conversation | **fixed** — capped at `MAX_TRAJECTORY_WINDOW = 240` |
| Web | subagent mirror `ThreadViewState.entries` | whole conversation | **fixed** — `boundTranscriptEntries(..., keepTail)` on every append and prepend |
| Web | `deliveryStates`, `jobs`, `tasks` | per id | bounded by live objects, not history |
| TUI (old client, deleted) | `TuiState.transcript` / `messages` / `notices` / `errors` / `tools` / `tasks` | whole conversation, except live-object-keyed ones | this was fixed in the old client by `_MAX_STATE_*` caps plus server-backed paging; the modules are gone |
| TUI (current) | `Timeline` entries (`tui/timeline.py`) | whole conversation | **open** — the snapshot is the full server history and nothing evicts; see `tui-rewrite-spec.md` §5.16 |
| TUI (current) | rendered widgets | whole window | **fixed** — the view mounts one planned window (`timeline.window(size=...)`), never one widget per event |

The *rendering* was already windowed in both clients; the gap was that the
*data* stayed full-fidelity, and reducer paths copied the whole array per event
(`[...state.entries, entry]`, `.map()` over entries for tool updates).

### TUI (old client, deleted): how the index keys were made safe

`TuiTranscriptEntry.key` is not a stable id: it is the decimal index into the
backing list (`client.py`, e.g. `str(len(self.messages) - 1)`), and
`TranscriptSurface` resolves an entry by that index. Trimming the backing lists
therefore has to keep three things aligned:

1. **Keys.** `_trim_payloads` drops the front `excess` payloads and renumbers
   the transcript keys of that kind by `-excess`, dropping the entries whose
   index went negative. Any surviving key still resolves to the payload it was
   created for. Trimming runs in batches (`_TRIM_SLACK`), so the renumber pass
   is amortized O(1) per event.
2. **The mounted window.** The surface holds `window_start`/`window_end` as
   absolute positions in the same transcript. The state publishes monotonic
   `evicted_transcript`/`evicted_messages` counters; the surface subtracts the
   deltas, dropping index-keyed widget caches for evicted payloads. A window
   that *overlapped* the evicted region (the reader had scrolled back into it)
   holds content the client no longer has, so it is re-mounted from the tail.
3. **The streaming index.** `_streaming_assistant_index` is shifted by the same
   delta, so a live stream keeps addressing its own message.

Tool eviction is the exception: tools are keyed by `tool_call_id`, so dropping
the oldest terminal tools leaves their transcript entries in place. Those
entries render nothing once the payload is gone (`widget_for_entry` returns
`None`), and the transcript's own cap reclaims them.

### Bounded work per update

`TranscriptSurface.sync` mounts `max(window_end, end - max_mounted_entries) ..
end`, i.e. at most one window, so a burst of events costs one window instead of
one widget per event. Entries skipped this way remain in the state window and
are mounted on demand if the reader scrolls back. Measured by a test that
records every mount width: a 150-entry backlog with no render in between mounts
100 widgets, and the assertion fails (150) when the bound is removed.

## Protocol capability (as built)

- History paging is **backwards only**: `GET /threads/{t}/history?limit=N&cursor=C`
  returns `messages[end-limit:end]` and `next_cursor` for the page start.
- Cursor = base64 of `[1, <surface_revision>, <offset>]` (`core/history.py:369-424`).
  The revision is regenerated on every derived-surface mutation, so any history
  mutation invalidates every outstanding cursor. Cursors are opaque to clients.
- Trajectory paging mirrors this (`page_trajectory`), and
  `SessionTrajectoryMessage.position` is a stable 1-based position on the
  append-only trajectory surface.
- There is **no forward (newer) cursor** and no "newest position"/"revision"
  exposure.

## Architecture: windowed, cursor-anchored state

Both clients converge on one model:

```
WindowState {
  entries: bounded ordered list (MAX_WINDOW entries)
  oldest_cursor: cursor for a page ending just before entries[0]   # paging up
  at_tail: bool                 # entries[-1] is the newest known
  pending_newer: int            # live entries dropped while not at tail
  revision: str                 # server surface revision; change => reset
}
```

Rules as implemented on the Web (`runtime.ts`):

0. `boundTranscriptWindow` is the **single choke point**: `runtimeReducer` wraps
   the action reducer and enforces the caps after *every* action, so no future
   action can reintroduce unbounded growth. Two invariants hold everywhere:
   `entries.length <= MAX_TIMELINE_ENTRIES`, `trajectory.length <=
   MAX_TRAJECTORY_WINDOW`.
1. **At tail**: append live entries; when `len > MAX_TIMELINE_ENTRIES`, evict from
   the front and advance `windowAnchor` to the position of the oldest *retained*
   record, so paging up continues exactly where the window now starts.
   `loadEarlier` therefore sends `before=<windowAnchor>`, not the stale
   `historyCursor` (whose offset the eviction invalidated).
2. **Scrolled up**: fetch older pages on demand (existing backwards paging) and
   evict from the **back** so the window stays bounded; stop materializing live
   events, counting them instead (`pending_newer`).
3. **Return to tail**: re-fetch the newest page (`cursor=None`) and replace the
   window. This is the reason no forward cursor is strictly required.
4. **Mutations**: `history_updated`/`compaction` trigger a fresh newest page
   (`refreshTrajectory`); the window is replaced rather than patched, and
   `atTail`/`pendingNewer` reset.
5. **In-place updates** (streaming deltas, tool status) only apply to entries
   inside the window; for entries outside it, drop the update — a re-fetch
   restores the authoritative record.
6. **Per-event work** is bounded by `MAX_WINDOW`: bounded arrays with `slice`
   trimming and an `id → index` map for tool updates, never a scan of history.
   `bounds` are applied once per action, not per append site.

### Anchors the client cannot infer

`windowAnchor` is `null` in two distinct situations, and the client must tell
them apart:

- nothing loaded yet → fall back to `historyCursor`;
- the retained window is entirely **live** output (no durable position yet) →
  paging back from it is meaningless, so `loadEarlier` re-anchors on the newest
  page first and the next request pages from a known position. Returning the
  newest page is the only correct move here: the skipped records are *newer*
  than the anchor, and forward paging (`after=`) does not exist.

Protocol addition (built): the trajectory surface accepts a **positional
anchor** and reports the tail.

- `GET /threads/{t}/trajectory?limit=N&before=P` returns the page ending at
  `P - 1` (exclusive 1-based trajectory position). The opaque cursor chain only
  walks backwards from the newest page, so a client that evicted its oldest
  entries could not otherwise fetch from where its window now starts without
  replaying the whole trajectory.
- `newest_position` is returned on every page, so a client knows whether it
  holds the tail and can re-anchor without walking cursors. (Returned by the
  server and typed in the Web client; the Web window currently re-anchors from
  the page contents alone.)
- `cursor` and `before` are mutually exclusive; an anchor outside the current
  record count fails as `HistoryCursorInvalid`.
- Positions are stable because the trajectory is append-only
  (`state.next_position = len(records) + 1`), unlike the derived-surface cursor
  whose revision changes on every mutation.

## Stress coverage required

> The TUI bullets in this section name tests and modules that were deleted with
> the old client (`test_tui_client.py`, `bench/test_tui_event_throughput.py`).
> They are kept as the record of what that implementation was verified against.
> The current TUI suite is `XBotv2/tests/tui/`; which parts of this list it does
> and does not yet carry is recorded in `tui-rewrite-spec.md` §5.16.

Protocol (Python):

- large history: page a 10k-node conversation backwards and confirm bounded
  page sizes, cursor validity, and revision-invalidation behaviour;
- trajectory paging parity with history paging;
- `tests/bench/test_history_paging_stress.py`: paging a 5 000-record trajectory
  backwards must visit every position exactly once in 25 bounded pages, and the
  positional-anchor walk must find exactly what the cursor walk finds, under a
  total-time ceiling that catches per-page work scaling with history length;
- event-stream throughput and replay/reconnect with cursor expiry;
- a memory and per-page-time ceiling assertion (bounded, not "informational").

Rendering (Web vitest + Python headless Textual):

- **done** `runtime.test.ts` "transcript window": 4 000 live events keep
  `entries.length <= MAX_TIMELINE_ENTRIES` and the newest content retained;
  8 older pages keep both windows capped; repeatedly paged-back windows stay
  contiguous (no gap); live events while scrolled back leave `entries`
  identical and only move `pendingNewer`; a refresh replaces the window and
  resets `atTail`/`pendingNewer`; a page requested for a moved window is
  dropped; the 1 000th event costs no more than the first 1 000.
- **done** `Timeline.test.tsx` "rendering window": 10 000 retained entries mount
  a bounded number of DOM nodes with the newest content present, and mounting
  again does not grow the document.
- **done** TUI state window (`test_tui_client.py`): evicting 3 000 messages
  keeps the retained lists at the cap, the newest payload retained, and every
  retained transcript key resolving to its own payload; notices/errors capped;
  pending tools never evicted; the streaming index still addresses the
  streaming message after eviction.
- **done** TUI rendering window (headless Textual, `bench/test_tui_event_throughput.py`):
  a burst of 200 events and a 150-event no-render backlog each materialize at
  most one window, the mounted widget count stays at the cap, and every mounted
  body shows retained content while the newest answer is present. The
  mount-width assertion is the falsifying one (fails at 150 without the bound).
- **done** TUI paging (`test_tui_client.py` "transcript pages older history"):
  scrolling past the retained front consumes the anchor page (all duplicate ids
  skipped), asks again from its cursor, inserts the older page, mounts it,
  reports `at_tail = False`, and re-anchors on a fresh snapshot when the reader
  returns to the bottom.
- **done** `prepend_history` unit tests: overlapping pages contribute only the
  unseen records, the window stays bounded by evicting the newest end, retained
  keys stay addressable, live output is counted while in history, and
  re-anchoring restores the live path.
- **done** TUI memory ceiling (`test_tui_client.py` "memory is bounded by the
  window"): `tracemalloc` peak for a 20 000-message session is within a bounded
  factor of a 2 000-message one, i.e. retained memory does not track session
  length.
- **done** TUI runtime attribution: an injected turn renders as a provenance
  notice (never as typed input) both from a resumed snapshot and from the live
  `message` frame.
- **done** TUI full cycle (`test_tui_client.py` "cycle neither duplicates nor
  loses records"): with contiguous server pages, a fully retained page
  contributes nothing and the client asks again from its cursor; the older page
  is prepended and the window stays contiguous and duplicate-free; live output
  arriving while the reader is inside history is counted, not appended; and the
  snapshot re-anchor brings that output back.  The newest entries are paid for
  by the prepend (`evicted_transcript_tail > 0`).
- **open** the same cycle against a *live* event stream (the test drives state
  events directly rather than through the transport).

### TUI (old client, deleted) paging: both directions, on demand

The main transcript had no paging at all: the session snapshot seeds it with a
160-message page and `_load_earlier_replay` only walked `state.transcript`.
With the window in place, paging is what makes evicted history reachable again.

- `TuiState.at_tail`/`pending_newer` give the window a direction.  While the
  reader follows the tail, live output is appended and the oldest payloads are
  evicted; while they are inside history, live output is **counted** in
  `pending_newer` and eviction comes from the newest end instead.
- `prepend_history(messages)` inserts an older page at the front, **skipping
  messages whose `message_id` is already retained**.  This is what lets the
  first fetch use the *newest* page as its anchor: the page overlaps the window,
  the overlap is skipped, and only the genuinely older records are inserted —
  so the TUI needs no positional anchor, where its payloads carry no trajectory
  position.
- `_trim_tail` evicts the newest entries by popping the transcript from the end
  and dropping each entry's payload when it is the last of its container.
  Removing the newest element of an index-keyed list never invalidates the
  surviving keys, so a paging reader costs no renumbering.
- `_load_earlier_replay` fetches when the front of the window is reached
  (`_extend_history_backwards`, bounded to `_HISTORY_PAGE_ATTEMPTS` fetches per
  scroll); `_load_newer_replay` re-anchors on a fresh snapshot when the reader
  reaches the end of a window that is no longer at the tail.
- The surface tracks `inserted_transcript`/`inserted_messages` (window and
  index-keyed caches shift **up**) separately from `evicted_transcript` (front
  eviction) and `evicted_transcript_tail` (the window simply ends earlier).  A
  prepended index has no widget yet, which is why its cache entry must not be
  reused.

Focus bug this surfaced: `_restore_composer_focus` pulled focus out of a
transcript block whose body the reader had just focused (focusing a block is
what expands it and emits `Toggled`). It now returns early when the focused
widget is a `BoundedText`, so scrolling a block keeps focus.

## Collapsible blocks (old client, deleted)

> Kept as the record of the paged block window that existed then, because its
> rules came from bugs that were visible in use. The rewritten client renders
> the same idea differently (`tui/view/blocks.py`): a Textual window capped at
> `BLOCK_MAX_LINES` with a preview line, per-block `ctrl+e` expansion, and
> native scrolling -- so the padding rule below belongs to that implementation,
> not to this one.

## Collapsible blocks (reasoning, tool details)

A block window (`BoundedText`) is a fixed-height view over wrapped rows.  Rules,
each of them a bug that was visible in use:

- **Constant height while scrolling.**  The window rendered only the rows left
  below the cursor, so a block shrank to its last row as the reader reached the
  end.  A scrolling block now pads its page to `max_rows`; a block that fits
  stays compact, because it cannot scroll.
- **The end is a full page, not the last line.**  Scrolling stops at the cursor
  whose page ends with the final row (`_tail_cursor`), so the bottom shows the
  same number of rows as anywhere else and reports e.g. `2–8 of 8 lines`
  instead of `8–8 of 8 lines`.
- **The last page is computed from wrapped rows.**  `_tail_cursor` starts at the
  last *rendered* row of the final logical line: starting at the line's first
  wrapped row left the window short of the end (`at_end` false) for long lines.
- **A cursor found before the width was known is clamped.**  The tail position
  depends on how the last line wraps, so a cursor computed at the default width
  pointed past the end once the block was laid out, rendering an *empty* window
  inside a full-height block.  `_clamp_cursor` runs on every render, and a
  reflow re-establishes the intent below.
- **Whole content is read from the top; only growth pins to the end.**  A
  finished tool result is shown from its first row (`_top_anchored`), while a
  streamed block keeps following its last rows (`_follow`).  A reflow keeps
  whichever intent the block had, and scrolling by the reader clears the top
  anchor.

Covered by `test_expanded_block_keeps_one_height_while_scrolling`,
`test_scrolling_to_the_bottom_shows_the_last_full_page`,
`test_block_with_one_extra_line_scrolls_to_a_full_last_page`,
`test_a_short_block_shows_every_line_and_does_not_scroll`,
`test_a_wrapped_final_line_never_renders_an_empty_window`, and
`test_textual_app_replays_tool_permission_sequence_without_swallowing_messages`
(the last one fails when the clamp is removed).

## One transcript implementation for both views

The main transcript and the read-only subagent view used to be two programs:
`TranscriptSurface` plus an app-side paging implementation for the main view,
and a second implementation inside `ThreadView` (`load`/`prepend_items`/
`catch_up`/`load_older_window`) with its own state building.  They drifted, and
the subagent view rebuilt its whole surface on every 0.75 s poll, which is why
it flickered.

`AgentTranscriptPane` is now the single implementation: one surface, one window,
one paging path (held window first, then the source), one position-keeping rule,
one record-to-state conversion.  The two views differ only in what they inject:

| | main transcript | subagent view |
|---|---|---|
| container | `#transcript` | `#thread_transcript` |
| records | live events + session snapshot | polled/streamed trajectory |
| `fetch_older` | `read_thread_history(cursor=...)` | `read_thread_trajectory(cursor=...)` |
| `reanchor` | session snapshot (`refresh_baseline`) | its own newest page |

Rules the shared pane enforces:

- **Reading up exhausts the held window first**, then asks the source; the
  position is held either way, so a page-up stays a scroll instead of jumping to
  the top.  The compensation is measured after layout, because widgets mounted
  above the viewport have no height yet at mount time.
- **Reading down steps one window**, then follows the tail, and only re-anchors
  when the window reaches the end of what the client holds.  The main
  transcript's re-anchor is a *snapshot*: walking a history page instead (as an
  earlier revision did) replaced the transcript with that one page and dropped
  everything else the client had.
- **The window is bounded in both directions**: entries mounted above are paid
  for by the newest end (`trim_mounted_to_cap`), so long scrolls cannot grow the
  mounted set.
- **A page fetched before the window was replaced is dropped** (`state.revision`
  check), so a session switch or re-snapshot cannot splice rows from two windows
  together.

## Subagent view refresh

`_poll_thread_view` used to call `ThreadView.load` every 0.75 s, which built a
new `TuiState` and a new surface and re-mounted every widget.  A poll now
compares the page with what the client holds by trajectory position:

- nothing newer -> the DOM is not touched at all;
- newer records only -> appended one by one through the same per-record path a
  restore uses (`TuiState.apply_history_item`), so a polled page and a restored
  page produce identical state;
- a rewritten page (compaction, or a window that slid past what the client
  holds) -> rebuild, because splicing it would mix two windows.

Polling is also skipped entirely while the reader is inside history, so a
refresh cannot move the window under them.

## Escape in the read-only view

`action_clear_input` interrupted the main turn whenever one was running -- even
while the subagent view was on screen, where the reader is looking at another
thread entirely and the pane is read-only.  Escape now leaves the view and does
nothing else; in the main transcript it still interrupts.

## WebUI: missing sessions and stuck spinners

- **404 banner over a working chat.**  Activation fetched six resources in one
  `Promise.all`, so any of them returning 404 (a session deleted elsewhere, a
  server restart) rejected the whole activation and reported a banner even
  though the session was already open and its event stream attached.  Only the
  thread list and the trajectory baseline are essential now; the optional panels
  degrade to empty.  A 404 from a background request is treated as "this session
  is gone" and recovered by re-opening it (`isMissingSessionError`), never as a
  banner: the user did not make that request.
- **Spinner that never stopped.**  `turnRunning` is cleared by `turn_finished`,
  so a terminal frame lost across a reconnect left the spinner up over a
  finished reply.  While a turn is running the client now asks the server what
  the thread is actually doing every 5 s and adopts that (`thread_synced`), which
  clears the spinner from the authoritative state.

## Surface bookkeeping (fixed)

## Surface bookkeeping (fixed in the old client, deleted with it)

> The two defects below were in `TranscriptSurface`, which no longer exists. The
> current client anchors the reader by entry id instead of compensating for
> measured heights (`tui/view/plan.py`, `tui/view/transcript.py`), which is the
> structural fix for the same class of drift.

`test_focused_block_scrolls_with_keys_then_hands_off_to_the_transcript` was
flaky (about 1 run in 6, reproduced on unmodified `main`), failing on
`scroll_y == 0` after `scroll_home`. Tracing the transcript's scroll calls
showed the viewport being moved by the compensation lambda in
`_load_earlier_replay`. Two product defects behind it:

1. `TranscriptSurface.mount_entries` appended every widget it *resolved* to its
   return value and to `mounted_entry_widgets`, including widgets already on
   screen. The caller therefore measured an "inserted height" for rows that
   never appeared, and the mounted list gained duplicates. It now reports and
   tracks only the widgets it actually mounts.
2. Window bounds were derived from the widget *count*
   (`window_start = window_end - len(mounted_entry_widgets)`), which assumes
   every entry renders a widget. Entries whose payload is gone render nothing,
   so the count drifted and the window claimed ranges it already showed -- and
   `_load_earlier_replay` kept re-mounting them. The surface now keeps
   `_mounted_entry_indices` beside the widget list and derives
   `window_start`/`window_end` from the entry indices that are really mounted.

With those exact, the remaining defect was the compensation itself: at the very
top of the transcript (`scroll_y == 0`) the entries just mounted *are* what the
reader scrolled back for, and shifting the viewport by their height pushed them
off screen again. Compensation is now skipped at the top.

Regression tests: `test_mount_entries_reports_only_newly_mounted_widgets` (fails
with the old return value and duplicate bookkeeping) and
`test_loading_earlier_entries_at_the_top_keeps_the_reader_at_the_top` (fails at
`scroll_y == 95` without the compensation guard). The previously flaky test ran
20/20 green after the fix (4/12 before).

## Known correctness bugs to fix alongside

1. ~~Web memo chain is broken~~ — fixed in three layers, each with a test:
   `MessageItem` memoizes the parsed Markdown element and compares handler
   *presence* rather than identity; `useXBot` resolves the session for
   `onRetry`/`onBranch`/`onLoadOlder`/`onLoadLatest` through `liveStateRef`
   instead of closing over `state.current` (which is replaced on every usage or
   status-slot event), so those callbacks are stable; and `Timeline`'s memo
   then stops the render before it reaches the entries. The Timeline test is
   falsifiable in both directions: stable props do not re-render the entries,
   and an unstable handler identity does.
2. ~~TUI live `message` path treated injected (runtime-attributed) turns as
   typed human input~~ — fixed in the old client, and still an invariant of the
   current one: `tests/tui/test_state.py::test_injected_history_turn_is_a_notice_not_typed_input`.
3. ~~Web `entries` and TUI `transcript` are never trimmed~~ — fixed on the Web;
   **open on the TUI**: the new client renders a bounded window but retains the
   whole history (`tui-rewrite-spec.md` §5.16).
4. ~~TUI `notices`/`errors` grow per message~~ — the old client capped them; the
   new client has a single `Timeline` for every entry kind and no cap (same §5.16
   item).
