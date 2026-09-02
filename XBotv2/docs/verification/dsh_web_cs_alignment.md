# DSh Web and C/S alignment

This document is the acceptance ledger for aligning XBot with the browser and
client/server behavior in the checked-in DeepSeek Harness reference at
`output/deepseek-harness`. It records product behavior, not visual resemblance
or test counts. Update a row only when the referenced runtime evidence exists.

Status values are:

- `verified`: exercised through the assembled application and inspected at the
  owning durable or user-visible surface;
- `partial`: a usable path exists, but one or more listed semantics are absent;
- `missing`: no production path provides the behavior;
- `decision`: deliberate divergence requiring an explicit product decision.

## Acceptance order

1. Runtime logs must explain an assembled request from HTTP admission through
   context construction, model response, Tool execution, persistence, and
   teardown without exposing message or credential contents.
2. Persistence must survive process restart and preserve one authoritative
   representation for history, inbox, plugin state, metadata, usage, and
   artifacts. History mutations and forks must not introduce duplicate state.
3. The C/S runtime must own reconnect, paging, resource projection, commands,
   interactions, and session/workspace lifecycle independently from React.
4. The browser must consume those capabilities without reconstructing server
   state or accumulating unbounded render work.

## Runtime log acceptance

The acceptance run uses the assembled HTTP server, real XCore plugin tree,
filesystem persistence, and MockLLM only at the external model boundary. Its
log file and data directory are retained under a caller-selected temporary
directory for inspection.

| Requirement | Status | Required evidence |
| --- | --- | --- |
| Application and XCore plugin lifecycle, dependency waits, service registration, unload | verified | Ordered DEBUG records from boot through teardown |
| HTTP method, route, status, duration, and `http_request_id` | verified | One correlated create/message/resource/close sequence |
| Session/thread open, resume, turn ownership, interrupt, close, and fork | verified | Stable `session_id`, `thread_id`, and domain `request_id` fields |
| Tool registration, model-visible selection, injection, start, finish, denial, and failure | verified | Tool names and counts without argument or result contents |
| Context composition and injected sections | verified | Section names, message/tool counts, token estimate, and duration |
| Model request and response | verified | Provider/model, stream mode, message/tool counts, latency, finish reason, and normalized usage |
| Persistence operations | verified | History append/replace/load, inbox reconciliation, metadata, plugin state, artifact, and lifecycle records |
| Configuration changes | verified | Owning layer, changed keys, committed outcome, and no values containing credentials |
| Levels, category overrides, rotation, redaction, and exception location | verified | Real files at INFO and DEBUG plus one controlled failure |
| Content safety | verified | Search proves prompts, user text, Tool arguments/results, attachment bytes, and secrets are absent |

No row becomes `verified` from unit tests alone. The acceptance report must
name the run directory, command, event counts, missing events, and inspected
correlation chain.

## Persistence acceptance

| Requirement | Status | Required evidence |
| --- | --- | --- |
| One canonical current history with ordered Message parts | verified | Inspect `messages.jsonl`; no duplicate derived content/tool-call fields |
| One atomic pending inbox snapshot reconciled by stable input id | verified | Crash/restart with pending input and inspect `inbox.json` before and after commit |
| Namespaced plugin state in `plugin_state/state.json` | verified | Goal, Todo, and usage survive close and process restart without adjacent plugin files |
| Typed content-addressed artifacts | verified | Uploaded image/file and Tool artifact survive restart and resolve through the public artifact endpoint |
| Strict thread/session metadata | verified | Workspace, provider, Agent, parent, title, and lifecycle restore without inferred fallbacks |
| Clear, undo, regenerate, and compact replacement semantics | verified | Physical records match the effective history exactly after every mutation |
| Fork isolation | verified | Fork initially matches the source, then mutations on either side do not cross-write |
| Loud corruption handling | verified | Invalid JSON, incomplete JSONL, invalid positions, and malformed typed records refuse resume |
| No second serialization protocol | verified | Production code uses MessageRecord, ThreadPersistence, StateService, and ArtifactStore only |
| Restart fidelity | verified | A second server process reproduces history, usage, Todo, Goal, artifacts, and session/workspace listing |

## Accepted runtime evidence

The retained run at `/tmp/xbot-runtime-acceptance-20260831-31` was produced by:

```console
PYTHONPATH=. .venv/bin/python scripts/accept_runtime.py \
  --root /tmp/xbot-runtime-acceptance-20260831-31
```

Its `report.json` records six distinct process IDs, including one process that
terminates without application teardown after persisting `accept-pending`.
The next process resumes and commits that input automatically. A first-class
Workspace is created before the session, then restored with its session
membership by the second process. Session rename, archive state, membership,
and manual session order are written through typed resources, inspected in the
shared Workspace snapshot, and observed by later processes. The run also
exercises Goal, Todo, usage, an
attachment, a child thread lifecycle, manual
compaction, undo, server-side regeneration, fork isolation, clear, a controlled
provider exception, Tool success/denial/failure, active and idle interrupt, and
four corrupt copies that all refuse HTTP resume.

The separate `logging-policy` files prove category DEBUG under an INFO base
level, suppression of unrelated DEBUG, field redaction, and an actual 5 MiB
rotation. The runtime log contains every event listed by the report and none of
the unique user, pending-input, attachment, failure-input, or API-key markers.

## DSh reference surfaces

The primary reference is the current source, not `output/dsh_analysis.md`,
which describes an older architecture. Relevant DSh owners include:

- `packages/client/runtime`: React-free Connection, Session, Workspace,
  generation, paging, projections, and reconnect state;
- `packages/client/ui-conversation`: conversation nodes, isolated streaming
  tail, queue, composer, stats, context pressure, and attachment intake;
- `packages/client/ui-workspace` and `ui-sidebar`: workspace/session tree,
  search, ordering, archive, blank-session reuse, and navigation;
- `packages/client/ui-commands` and `ui-input-trigger`: host command directory,
  exact execution claims, fuzzy discovery, popups, and input ownership;
- `packages/client/ui-tool`, `ui-subagent`, `ui-user-questions`, `ui-plan`,
  `ui-goal`, `ui-deliverables`, and `ui-trajectory`: composed conversation
  extensions;
- `packages/api/gateway`, `packages/client/connection`, and
  `packages/host/webserver`: typed unary RPC, cancellation, trust, transport,
  and event-stream ownership;
- `packages/core/session` and `packages/session/*`: append-only event authority,
  projections, JSONL/SQLite providers, query, export, and telemetry.

## C/S alignment matrix

| DSh behavior | XBot status | Gap or acceptance condition |
| --- | --- | --- |
| React-free client object layer owns sessions, event windows, reconnect, and paging | partial | A React-free event connection owns cancellation and bounded reconnect; session orchestration, paging, and projections remain in `useXBot` |
| Typed unary operations are separate from session event streams | verified | FastAPI resources/operations and SSE are separate; retain this separation |
| Generation-scoped reconnect rejects stale responses and frames | verified | Workspace catalog and active Session events use pre-read cursors, bounded replay, and generation checks; main turns are Session-owned and replay through the same sequence |
| First-class Workspace registry and ordering | verified | Durable create/list/rename/delete/order and Session-event-driven membership share the Workspace service and survive process restart |
| Incremental session/workspace list frames and multi-client synchronization | verified | Typed post-commit XCore events feed a bounded Workspace catalog SSE window; real HTTP and browser clients observe create/delete without refreshing |
| Session search with ranked snippets | missing | Sidebar performs local metadata filtering only |
| Archive, rename, ordering, and blank-session reuse | verified | Session rename, global archive/restore, Workspace and per-Workspace session order are durable; desktop/mobile browser tests prove New reuses only a Workspace-accounted, unarchived session whose server projection is blank |
| Bounded event window plus history paging | partial | Persistence-owned history paging and the complete active Session stream are cursor-based; the runtime window is not yet a durable event query/export store |
| Durable domain projections independent of transcript | partial | Todo has a typed projection; usage/status/goal do not share one projection protocol |
| Commands fail closed rather than degrading to prompts | verified | Known server/client commands use explicit paths; unknown slash input is rejected |
| Pending interaction identity survives reconnect | partial | Interaction events and waiters now outlive the POST response and replay by cursor; multi-client arbitration still needs acceptance |
| Cancellation and stale handle behavior | partial | POST loss no longer cancels a turn and explicit interrupt does; withdrawn capability handles still need stronger semantics |
| Export/query over durable session events | missing | Message history endpoint is not a general session-event query/export surface |

## Web alignment matrix

| DSh behavior | XBot status | Gap or acceptance condition |
| --- | --- | --- |
| Workspace/session sidebar with search and row actions | partial | Durable grouping, ordering, rename/removal, archive/restore, fork/delete, multi-client updates, concurrent active-session navigation, and server-directory selection for New exist; ranked content snippets do not |
| Command trigger owns fuzzy discovery, exact dispatch, argument and popup flows | partial | Caret/span detection, fuzzy discovery, menu pick, and exact dispatch are owned explicitly; popup-select decorations remain absent |
| Conversation nodes isolate domains and streaming tail | partial | Message, Tool, context-injection, and notice seats are separate and the streaming tail stays isolated; the central reducer still assembles every domain |
| Queue shows, edits, deletes, and strictly steers pending messages | verified | The Agent inbox is the sole durable authority; typed list/update resources, replayed snapshots, resume projection, and the desktop/mobile QueueDock cover edit, remove, and next-step retarget without premature transcript insertion |
| Paste and whole-page drop share bounded attachment intake | partial | Paste and whole-window drop share one intake path, and message images open in a focus-restoring lightbox; count/byte limits, aggregate validation, and server-advertised rejection copy remain absent |
| Tool-specific presentation with generic fallback | partial | File, terminal, search, Todo, applied write/replace DiffBlock, downloadable Tool artifacts, and generic cards use lazy details. Long values and diffs use bounded head-tail previews with full copy/expand; patch before/after, location opening, and details navigation remain absent |
| Subagent hierarchy, running state, and read-only replay | partial | Threads are selectable; descendant tree, counts, and specialized replay are absent |
| Model selection, context pressure, cache, timing, and throughput | partial | Provider/model/effort, cumulative usage/cache counts, context occupancy, LLM/Tool wall time, TTFT, decode time, and decode throughput are durable and visible; detailed context-section breakdown is absent |
| Settings, theme, locale, plugin inventory, and permission presets | missing | No composed settings surface or host-backed preference model |
| Goal, Todo, plan, user questions, jobs, deliverables, and trajectory compose independently | partial | Several fixed panels exist; there is no replaceable UI composition seam or trajectory/deliverables view |
| Message actions: copy, feedback, branch/regenerate, produced files | partial | A DSh action strip owns copy, regenerate, and finalized-tail session fork; feedback, arbitrary historical-point fork, and produced-file summary remain absent |
| Long histories and long single messages stay responsive | partial | History/DOM and collapsed Tool output are bounded; accumulated streaming Markdown and explicitly expanded large values still require full-value parsing |
| Keyboard, focus, reduced motion, mobile, and screen-reader flows | partial | Mobile E2E exists; focus trapping, full keyboard flows, reduced motion, and accessibility acceptance remain |

## Frontend port ledger

This ledger distinguishes source-level adoption from visual approximation.
`ported` means the DSh component's ownership and interaction model is present
in XBot behind a protocol adapter; a token or selector override alone remains
`partial`.

| DSh owner | Status | XBot owner and remaining work |
| --- | --- | --- |
| `ui-layout/AppFrame` | ported | `DshAppFrame` owns responsive sidebar concession, the 56 px rail, persisted width, pointer-captured resize, and center-column clipping; the unused DSh details column is intentionally absent until XBot exposes a trajectory/details surface |
| `ui-sidebar/SidebarRoot` | partial | XBot uses DSh wide/rail geometry, frozen-width settle/crossfade, rail search/refresh, pointer-scoped scrollbars, and the Workspace/Session region; each Workspace mounts at most five session rows until explicitly expanded. The footer settings seat and ranked search snippets remain |
| `ui-conversation/ConversationRoot` | ported | Conversation and sticky composer now share one vertical scroll owner; the transcript no longer scrolls independently from its footer |
| `ui-conversation/ChatView` | partial | Bounded paging and follow-bottom exist; Timeline now owns only the window and scroll while Message, Tool, context-injection, and notice domain seats render independently. The reducer still assembles the node sequence |
| `ui-conversation/InputBar` | partial | DSh capsule geometry, 14-line growth cap, context occupancy/details, attachment rail, caret/span-aware fuzzy slash popup, send/interrupt, paste, whole-window drop, and an authoritative editable QueueDock are present; mirror/backdrop reference decoration and server-advertised bounded intake are missing |
| `ui-jobs/JobListAction` | ported | Background jobs moved from the composer stack into a header trigger and bounded popover with stop controls |
| `ui-tool` | partial | Tool rendering is separated from Timeline and has DSh disclosure rows plus file, terminal, search, Todo, applied write/replace DiffBlock, downloadable result artifacts, and generic cards. Bounded output and diff surfaces keep long middles out of the DOM until explicit expansion and copy the complete source. Patch before/after, host-backed file opening, turn-level ProducedFiles, and trajectory inspection remain |
| `ui-conversation/MessageIconActions` | partial | Shared copy/regenerate/branch chrome is ported. XBot's current fork API branches only the current finalized tail, so arbitrary historical-point branch remains a server capability gap |
| `ui-input-trigger/MenuView` | partial | The menu and caret/span detector own fuzzy leading-slash discovery and focus-preserving picks outside `Composer`; popup-select command decorations and other trigger sources remain |
| `ui-workspace/DirectoryBrowser` | ported | The new-session flow has editable absolute paths, registered Workspace shortcuts, parent/home/hidden navigation, bounded directory results, cancellation, and desktop/mobile selection backed by XBot's read-only Workspace resource |
| `ui-primitives/Modal`, command and interaction overlays | ported | Command discovery, permission/user questions, clear, delete, new-session, and directory dialogs share the DSh light menu surface, mask, controls, and responsive bounds; selecting a command returns its editable invocation to the composer |
| `ui-settings-*`, locale, theme | missing | No settings document or host-backed preference resource yet |

The port keeps XBot's typed HTTP resources and Session/Workspace event streams.
DSh's Cordis runtime, remote-call protocol, and projection stores are not copied:
doing so would introduce a second C/S authority. Presentation components are
ported at their existing ownership boundaries and receive data from XBot's
React-free client controllers.

## Remaining Web gaps

The current browser is usable for the primary session workflow, but these
known gaps remain and must not be represented as DSh parity:

1. Move session activation, paging, and resource projection out of `useXBot`
   into a React-free session client with explicit per-session identities.
2. Add ranked session-history search, arbitrary historical-point branch, and
   complete subagent hierarchy/replay rather than metadata-only filtering and
   finalized-tail fork.
3. Add composed settings for theme, locale, plugin inventory, and permission
   presets; Todo labels and the remaining UI copy are not localized.
4. Add server-advertised attachment limits, detailed context-section
   accounting, patch before/after views, file-location opening, and a
   turn-level produced-files summary.
5. Complete keyboard/focus trapping, screen-reader, reduced-motion, and real
   browser/provider acceptance. Explicitly expanded very large Markdown or
   Tool values can still create high parse and DOM cost.
6. Add multi-client interaction arbitration and a durable event query/export
   surface; the current bounded process event window is for reconnect, not a
   general trajectory store.

## Frontend replacement gate

Do not preserve the current React implementation merely because individual
features can be patched into it. Replace or port the DSh client frontend when
either condition remains true after the C/S acceptance work:

1. transport/session/reconnect/projection logic cannot be removed from React
   hooks into one testable React-free runtime without adding another parallel
   state model; or
2. adding Workspace, queue, composed conversation domains, settings, and
   specialized Tool views would continue growing `App.tsx`, `useXBot.ts`, the
   central reducer, or the global stylesheet as product registries.

A replacement may reuse DSh source and structure subject to its license. It
must adapt to XBot's public C/S protocol rather than importing XBot server
internals or emulating missing server state in the browser.

The gate is currently triggered. The selected production surface is 5,902
lines (`useXBot.ts` 745, reducer 736, `App.tsx` 446, legacy global CSS 1,710,
DSh presentation CSS 978, API client 509, and six React-free owners totaling
778). The DSh port has removed 320 lines from the legacy stylesheet, but the
combined client surface has not yet contracted. XBot will therefore
port DSh's ownership boundaries incrementally and delete the displaced React
logic. Copying the DSh runtime wholesale is not the selected path: its Cordis,
remote-call, and event-log contracts are not XBot's public protocol, so a
literal copy would retain two incompatible C/S models. This decision must be
revisited now: extraction reduced hook/reducer ownership, but the total surface
grew while adding Workspace behavior and still contains parallel navigation
and projection logic. The next client phase must delete displaced React state;
if it cannot, port the DSh client shell and adapt it to XBot's public protocol.

## Change log

- 2026-08-29: Established the ledger from the current DSh source and XBot
  production paths. No logging or persistence row is yet accepted from this
  document alone.
- 2026-08-30: Accepted the assembled logging and persistence lifecycle across
  independent processes, crash recovery, strict corruption refusal, and log
  policy/rotation probes. The production-code audit found no direct plugin
  writes to thread history, inbox, plugin state, or artifact paths and no
  second Message serialization path; client history projection remains a
  transport concern rather than durable serialization.
- 2026-08-30: Added and accepted the durable Workspace resource through the
  assembled HTTP application and an independent restart. The browser now
  groups sessions by that resource and exposes ordering, rename, and removal.
- 2026-08-30: Closed the remaining log acceptance gaps with an actual denied
  filesystem Tool, a valid Tool invocation returning a domain error, and a
  provider blocked until an HTTP interrupt cancels its active turn.
- 2026-08-30: Triggered the frontend replacement gate, recorded the porting
  decision, and moved session-event cancellation/retry into the first
  React-free client owner instead of adding another hook-local controller.
- 2026-08-30: Moved Workspace and session-list baselines out of the conversation
  reducer into React-free observable owners. Added durable session rename and
  verified its thread-metadata representation across later process restarts;
  fixed default Workspace registration so corrupt session history cannot fail
  application boot before the responsible list/resume boundary.
- 2026-08-30: Added durable session archive/restore to the Workspace snapshot,
  accepted its physical representation across independent processes, and
  exercised the sidebar flow on desktop and mobile without deleting history.
- 2026-08-30: Persisted each Workspace's explicit session order, reconciled new
  and deleted members against the session authority, and proved manual order
  through the HTTP resource, physical snapshot, and desktop/mobile sidebar.
- 2026-08-30: Added a bounded Workspace-owned catalog event window with pre-baseline
  cursors, replay, expiry reset, and typed Session/Workspace post-commit
  frames. Workspace membership now subscribes to Session resource events
  through XCore instead of depending on a list request. Real socket and real
  browser tests proved multi-client create/delete updates and replay. Re-ran
  the complete multi-process runtime acceptance at `-28` after this write-path
  change.
- 2026-08-30: Made persisted assistant reasoning part of the public history
  projection. HTTP close/resume, TUI widgets, and desktop/mobile browser tests
  now prove that Thinking survives resume instead of existing only in the live
  event stream.
- 2026-08-31: Removed the independent Host-event package. The Workspace plugin
  now owns typed Session/Workspace catalog replay, while protocol modules only
  map DTOs/routes and XCore registration lives in `http/plugin.py`. Moved
  conversation paging into the history stores with opaque revision-bound
  cursors, and made HTTP, runtime mutation events, and ACP consume one
  transport-neutral replay projection.
- 2026-08-31: Added a per-runtime bounded replay window for accepted inputs,
  interactions, jobs, configuration/status changes, and background
  continuation events. Open responses provide the pre-snapshot cursor and
  Web/TUI/ACP resume from it.
- 2026-08-31: Moved main-turn ownership out of the POST response into the
  Session runtime. Main reasoning, tools, usage, interactions, and terminal
  events now share that replay sequence; official clients drain the POST view
  without feeding it into their UI mapper, so disconnect recovery is neither a
  duplicate delivery path nor an implicit interrupt.
- 2026-09-01: Began source-level frontend replacement rather than CSS-only
  approximation. Ported DSh's AppFrame geometry/resize owner, converted the
  conversation and composer to one scroll owner, and moved background jobs to
  a DSh-style header action. Added the frontend port ledger so source adoption,
  protocol adaptation, and remaining gaps cannot be reported as equivalent.
- 2026-09-01: Ported the DSh sidebar settle/rail, reasoning and Tool rows,
  header Jobs action, context meter, whole-window attachment drop, and image
  lightbox. The browser now enters a local submitting turn state before the
  first Session event, closing the navigation race between POST completion and
  `turn_started`; the mobile race probe passed five consecutive runs.
- 2026-09-01: Ported the shared DSh modal surface across command discovery,
  permissions, user questions, and destructive confirmations, then removed
  the displaced dark-theme dialog rules instead of retaining an override
  stack. Workspace groups now mount only five session rows until expanded and
  sidebar scrollbars remain quiet outside pointer interaction.
- 2026-09-02: Replaced Timeline's domain switch with independent conversation
  node seats, ported the shared message IconActions strip, and exposed branch
  on the finalized assistant tail through XBot's authoritative Session fork.
  Moved slash-menu presentation out of Composer and adopted DSh caret/span
  detection plus fuzzy ranking, eliminating URL/body false triggers and the
  displaced global menu/context-injection CSS.
- 2026-09-02: Ported DSh DiffBlock for successful changed `edit` calls whose
  explicit model-facing arguments contain a complete write or replacement.
  The settled Tool result remains authoritative, absolute resolved paths never
  enter the presentation, and failed, unchanged, malformed, and patch calls
  retain the generic result view rather than showing an inferred diff. Tool
  results now also retain their explicit artifact links across every card kind;
  this is deliberately distinct from unsupported turn-level ProducedFiles.
- 2026-09-02: Ported DSh's session StatsLine with server-recorded model, TTFT,
  decode, and Tool timing. Timing is persisted on the responsible messages;
  Session open, resume, terminal events, history mutations, paging, and
  compacted prefixes consume one durable aggregate instead of browser event
  arrival times. Context occupancy and connection status retain separate UI
  ownership.
- 2026-09-01: Replaced the browser's synthetic queue counter with the durable
  Agent inbox projection. Busy Web submissions now explicitly choose
  next-turn delivery, remain absent from transcript until claimed, and can be
  edited, removed, or retargeted to next-step through typed resources. Session
  resume, shared events, Python SDK, desktop, and mobile consume the same state.
- 2026-09-01: Ported DSh's bounded head/tail Tool-output behavior. Opening a
  long Tool card now mounts at most 16 lines and 20K characters by default,
  while explicit expansion and copy still operate on the complete result.
