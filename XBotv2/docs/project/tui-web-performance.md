# TUI and WebUI performance architecture

Goal: memory and per-update time stay bounded as a conversation grows. "Bounded"
means O(1) **with respect to conversation length** — work proportional to a fixed
window (a few hundred entries) is fine, work proportional to the whole history is
not.

## What was unbounded, and where it stands

| Client | State | Grows with | Status |
|---|---|---|---|
| Web | `RuntimeState.entries: TimelineEntry[]` | whole conversation | **fixed** — capped at `MAX_TIMELINE_ENTRIES = 240` by `boundTranscriptWindow` |
| Web | `RuntimeState.trajectory: TrajectoryItem[]` | whole conversation | **fixed** — capped at `MAX_TRAJECTORY_WINDOW = 240` |
| Web | subagent mirror `ThreadViewState.entries` | whole conversation | **fixed** — `boundTranscriptEntries(..., keepTail)` on every append and prepend |
| Web | `deliveryStates`, `jobs`, `tasks` | per id | bounded by live objects, not history |
| TUI | `TuiState.transcript: list[TuiTranscriptEntry]` | whole conversation | **fixed** — capped at `_MAX_STATE_TRANSCRIPT` (600) + slack 200 |
| TUI | `TuiState.messages`, `notices`, `errors` | whole conversation | **fixed** — front-evicted at `_MAX_STATE_MESSAGES` (400) / `_MAX_STATE_NOTICES` (200) / `_MAX_STATE_ERRORS` (100) |
| TUI | `TuiState.tools` | per live tool call | already bounded by `_MAX_STATE_TOOLS` (300) after this change; terminal tools are dropped oldest-first and running ones are never evicted |
| TUI | `TuiState.tasks` (jobs) | per live job | already bounded by `prune_finished_tasks` (3 s grace) |
| TUI | `TuiState._tool_transcript_keys` | per tool call | bounded by the tool cap |
| Web | `RuntimeState.deliveryStates` | per pending input | bounded by live inputs |

The *rendering* was already windowed in both clients; the gap was that the
*data* stayed full-fidelity, and reducer paths copied the whole array per event
(`[...state.entries, entry]`, `.map()` over entries for tool updates).

### TUI: how the index keys were made safe

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
- **open** up-scroll/down-scroll cycle against a live stream, asserting no
  duplicate and no missing record between the two fetches.

### TUI paging: both directions, on demand

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
2. TUI live `message` path treated injected (runtime-attributed) turns as typed
   human input — fixed; keep it covered by a test.
3. ~~Web `entries` and TUI `transcript` are never trimmed~~ — both fixed.
4. ~~TUI `notices`/`errors` grow per message~~ — both capped.
