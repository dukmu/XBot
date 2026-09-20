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
| TUI | `TuiState.transcript: list[TuiTranscriptEntry]` | whole conversation | **open** — see the index-key blocker below |
| TUI | `TuiState.messages`, `tools`, `notices`, `errors` | whole conversation | **open** — widget caches (`_MAX_MESSAGE_WIDGETS`, `_MAX_TOOL_WIDGETS`, `_MAX_MOUNTED_ENTRIES`) are bounded; the backing lists are not |

The *rendering* was already windowed in both clients; the gap was that the
*data* stayed full-fidelity, and reducer paths copied the whole array per event
(`[...state.entries, entry]`, `.map()` over entries for tool updates).

### TUI blocker (must be resolved before mirroring the Web model)

`TuiTranscriptEntry.key` is **not** a stable id: it is the decimal index into
the backing list (`client.py:416/441/564/722`, e.g. `str(len(self.messages) - 1)`),
and `TranscriptSurface` resolves an entry by that index. Trimming
`messages`/`tools`/`notices`/`errors` from the front would therefore silently
re-point every retained transcript entry at different content, and
`surface.window_start`/`window_end` are absolute indices into the same list.

Window the TUI state only after the transcript entries own their payload (or
after trim remaps every surviving index and shifts both window bounds). Doing it
in the other order corrupts the visible transcript.

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
- **open** TUI equivalent: headless Textual run asserting
  `len(state.transcript)` is capped and the mounted-widget count stays bounded
  (blocked on the index-key refactor above);
- **open** up-scroll/down-scroll cycle against a live stream, asserting no
  duplicate and no missing record between the two fetches.

## Known correctness bugs to fix alongside

1. Web memo chain is broken: `runtime.fork` is
   `useCallback([forkSession, state.current])` (`state/useXBot.ts:940`), and
   `state.current` is replaced on every usage/status-slot event. `onBranch` is
   passed to every assistant entry, so each such event re-renders every visible
   `MessageItem`; `MessageItem` renders `ReactMarkdown` without memoizing the
   parse.
2. TUI live `message` path treated injected (runtime-attributed) turns as typed
   human input — fixed; keep it covered by a test.
3. ~~Web `entries` is never trimmed~~ — fixed by the window above. The TUI
   `transcript` still has this defect (see the index-key blocker).
4. Web `deliveryStates` and TUI `notices`/`errors` still grow per id/message;
   they are bounded by live traffic rather than history, so they are lower
   priority, but they are not yet bounded.
