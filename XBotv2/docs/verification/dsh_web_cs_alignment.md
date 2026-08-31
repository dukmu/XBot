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

The retained run at `/tmp/xbot-runtime-acceptance-20260831-29` was produced by:

```console
PYTHONPATH=. .venv/bin/python scripts/accept_runtime.py \
  --root /tmp/xbot-runtime-acceptance-20260831-29
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
| Generation-scoped reconnect rejects stale responses and frames | partial | Workspace catalog and active Session shared events use pre-read cursors, bounded replay, and generation checks. Main turn SSE is still request-owned and lacks replay |
| First-class Workspace registry and ordering | verified | Durable create/list/rename/delete/order and Session-event-driven membership share the Workspace service and survive process restart |
| Incremental session/workspace list frames and multi-client synchronization | verified | Typed post-commit XCore events feed a bounded Workspace catalog SSE window; real HTTP and browser clients observe create/delete without refreshing |
| Session search with ranked snippets | missing | Sidebar performs local metadata filtering only |
| Archive, rename, ordering, and blank-session reuse | verified | Session rename, global archive/restore, Workspace and per-Workspace session order are durable; desktop/mobile browser tests prove New reuses only a Workspace-accounted, unarchived session whose server projection is blank |
| Bounded event window plus history paging | partial | Persistence-owned history paging and active Session shared-event replay are cursor-based; main turn events are not yet published through the replay window |
| Durable domain projections independent of transcript | partial | Todo has a typed projection; usage/status/goal do not share one projection protocol |
| Commands fail closed rather than degrading to prompts | verified | Known server/client commands use explicit paths; unknown slash input is rejected |
| Pending interaction identity survives reconnect | partial | Active runtime exposes pending requests, but reconnect baseline and multi-client mux semantics need acceptance |
| Cancellation and stale handle behavior | partial | HTTP abort/interrupt exist; withdrawn capabilities and reconnect generations need stronger semantics |
| Export/query over durable session events | missing | Message history endpoint is not a general session-event query/export surface |

## Web alignment matrix

| DSh behavior | XBot status | Gap or acceptance condition |
| --- | --- | --- |
| Workspace/session sidebar with search and row actions | partial | Durable grouping, ordering, rename/removal, archive/restore, fork/delete, and multi-client updates exist; ranked content snippets do not |
| Command trigger owns fuzzy discovery, exact dispatch, argument and popup flows | partial | Directory and exact dispatch exist; popup-select decorations and caret-aware trigger ownership do not |
| Conversation nodes isolate domains and streaming tail | partial | Timeline entries are typed, but one reducer/component still owns all domains and reparses growing Markdown |
| Queue shows, edits, deletes, and strictly steers pending messages | partial | Queue count/steer transport exists; message-level queue management UI is absent |
| Paste and whole-page drop share bounded attachment intake | partial | Paste works; drop, count/byte limits, aggregate validation, and actionable rejection copy are absent |
| Tool-specific presentation with generic fallback | partial | Generic collapsible cards exist; terminal/diff/location presentations and details navigation are absent |
| Subagent hierarchy, running state, and read-only replay | partial | Threads are selectable; descendant tree, counts, and specialized replay are absent |
| Model selection, context pressure, cache, timing, and throughput | partial | Provider/model/effort and usage exist; context breakdown, TTFT, throughput, and cache presentation are absent |
| Settings, theme, locale, plugin inventory, and permission presets | missing | No composed settings surface or host-backed preference model |
| Goal, Todo, plan, user questions, jobs, deliverables, and trajectory compose independently | partial | Several fixed panels exist; there is no replaceable UI composition seam or trajectory/deliverables view |
| Message actions: copy, feedback, branch/regenerate, produced files | partial | Regenerate exists; copy/feedback/per-message fork and produced-file summary are incomplete |
| Long histories and long single messages stay responsive | partial | History/DOM are bounded; accumulated Markdown and large Tool content still require full-value parsing |
| Keyboard, focus, reduced motion, mobile, and screen-reader flows | partial | Mobile E2E exists; focus trapping, full keyboard flows, reduced motion, and accessibility acceptance remain |

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

The gate is currently triggered. The measured production surface is 5,165
lines (`useXBot.ts` 730, reducer 728, `App.tsx` 436, global CSS 2,030, API
client 504, and six React-free owners totaling 737). XBot will therefore
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
  Web/TUI/ACP resume from it. Main `POST /messages` turn ownership remains a
  documented gap rather than being mislabeled as reconnect-safe.
