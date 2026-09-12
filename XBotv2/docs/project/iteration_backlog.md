# Architecture Iteration Backlog

This backlog tracks the next architecture iterations. It is ordered to reduce
ambiguity before large implementation changes.

## 0.3 Runtime resilience and compaction contract unification (2026-09-12, working branch)

Confirmed defects and fixes in this working branch:

- A provider stream exception after `turn_started` emitted an HTTP error but no
  shared `turn_finished` boundary. The session runtime now closes that
  lifecycle, and the TUI reconnects boundedly before reporting a terminal error.
- Trajectory recording reparsed the complete `messages.jsonl` for every event,
  making append latency quadratic. A process-local writer allocator now keeps
  positions synchronized across live stores; ordinary telemetry uses bounded
  delay flushing while message appends, surface replacements, and
  `compaction/*` markers remain forced. Multi-process ownership is still not
  claimed.
- Provider-confirmed context overflow now has a typed adapter boundary and a
  bounded compaction/rebuild/retry. Retry is allowed only after an append-only
  surface replacement changes the current nodes.
- Usage snapshot mutation is serialized inside `UsageService`: cumulative
  counters and the latest effective context projection cannot overwrite one
  another when a model-response usage update overlaps a compaction update.
  This lock is process-local and does not claim cross-process session ownership.

Contract unification in the same branch:

- One request anchor. `core.tokens.RequestAnchor` with `read_request_anchor` /
  `write_request_anchor` is the only reader or writer of the
  `xbotv2_request_*` metadata keys. The engine records it for every assistant
  message and compaction records the post-compaction projection through the
  same API, so provider/model/window matching and the honest `context_tokens`
  value are validated in one place instead of being re-derived per reader.
- One compaction metrics schema. `compact.protocol.CompactionMetrics` is the
  single definition; the parallel internal `TypedDict` is gone and the service
  builds, finalizes, and publishes that model. `compact/contracts.py` re-exports
  it for the proposal.
- One compaction reason vocabulary. `CompactionReason` is
  `automatic | manual | context-overflow`, and the `force_overflow` flag is gone
  because the reason already carries that decision. Wire events additionally
  carry `automatic`, so clients render the unrequested-compaction notice without
  re-deriving the vocabulary. This fixes a real defect: the TUI still compared
  `reason == "automatic"` while the service had renamed the threshold reason to
  `"pressure"`, so automatic compaction notices never appeared.
- One trajectory transaction query. `TrajectoryTransaction` lives in
  `core.history`, the history port exposes `open_transactions(transaction)`
  (replacing the three-argument `unmatched_event_ids`), and compaction declares
  `COMPACTION_TRANSACTION` once.
- Compaction recovery. An open transaction no longer disables compaction
  forever: the next attempt closes the bracket with a `compaction/end` record
  carrying an `aborted:` error, which is the only safe recovery because the
  surface is a fold of the trajectory.
- One tool-pairing implementation. `compact.history.tool_pairing_boundaries` is
  the single fold; the redundant private import and the two `tool_pairing_*`
  wrappers are gone.
- Typed provider call options. `ModelRequestOptions(max_output_tokens=...)`
  replaces the untyped `**kwargs` side channel through `BaseProvider.astream`,
  `ModelPort.astream`, and `LlmService.astream`; adapters read a validated field
  and Anthropic's required `max_tokens` stays a config-boundary error.
- Declarative provider error vocabulary. `core.providers.provider_context_overflow`
  classifies overflow from `types` / `codes` / `statuses` / `message_prefixes`
  that each adapter declares as data, replacing per-adapter inline `body`
  sniffing (including `startswith("prompt is too long")` inside the Anthropic
  adapter, now a declared 400-status prefix).
- Typed request-error outcome. `ModelRequestErrorOutcome` documents what a
  `model/request-error` listener asks for; the engine validates the hook result
  before acting on it.

Removed in this branch: incremental stream timing and streamed usage deltas.
The engine briefly published a pre-stream context estimate, per-delta TTFT and
decode metrics, and delta-ified provider usage snapshots, with the web and TUI
accumulating them client-side. That duplicated the terminal statistics channel
(`assistant_message.timing`, `turn_finished.session_stats`, provider
`usage_metadata`), broke the published turn event sequence, forced tests to
strip a `timing` field before comparing fixtures, and re-estimated the whole
accumulated text on every delta. Provider usage is once again reported only
through the finished `ModelResponse`.

Verification for this branch: `pytest XBotv2/tests/core XBotv2/tests/integration`
passed 880 tests; `npx vitest run src/state/runtime.test.ts` passed 38 tests and
`npx tsc --noEmit` is clean. These are the only claims made here. No real
provider, browser, multi-process, or real-TCP result is claimed: the socket and
browser smoke paths could not be exercised in this sandbox, and the retained
stress runner output under `scripts/stress/reports/` is local, git-ignored
evidence that predates the current source and therefore is not presented as
this branch's result.

## 0.2 Plugin-owned configuration contracts (2026-09-10)

- Built-in XBot plugins now declare configuration with Pydantic models in the
  owning plugin (`sandbox`, `permissions`, `coretools`, `compact`,
  `content_cache`, `browser`, `subagents`, and `mcp_plugin`). The configuration
  plugin only resolves overlays, persists mappings, validates through the
  declared model, and exposes its standard JSON Schema.
- Permission and sandbox entries use their direct plugin config shape. The
  configuration service now returns only the resolved generic `PluginTree`;
  the Agent composition boundary builds its small runtime projection and no
  longer defines any plugin model a second time.
- XCore remains dependency-free: its lifecycle validator recognizes any
  `model_validate` contract without importing Pydantic. XCore exports no
  schema DSL; external plugins must declare a Pydantic `Config` model.
- Core verification: focused configuration/policy/plugin tests passed; the
  non-socket HTTP integration set passed. Socket-backed TUI cases remain
  unavailable in the restricted test sandbox.

## 0.1 Live activity continuation (2026-09-10)

- The trajectory refresh path now holds raw `ServerEvent` objects while the
  append-only baseline is fetched, folds them once after the baseline, and
  discards the window on navigation. The reducer has a direct regression test
  for this ordering rather than only testing the projected result.
- Input delivery is a typed session event sequence: `input_accepted` and
  `input_claimed`. Queue and transcript projections keep the phase by stable
  message ID, including after reconnect; transcript `message` is the consumed
  boundary rather than a second queue event.
- The Web session page now keeps a DSH-style activity strip for every thread
  returned by the existing thread catalog, with model, message count, token
  usage, running state, and direct navigation. It is deliberately a summary
  aggregation; it does not invent independent event loaders for child threads.
- Verification for this continuation: 94 Web tests, production build, and
  focused queued/SSE integration tests passed. Socket-backed TUI tests remain
  dependent on host permission in the sandbox.

## 0. WebUI Stability Audit (2026-09-09, discovery and implementation status)

This section records the WebUI investigation and the first implementation
pass. It deliberately separates fixes that are now present from the remaining
DSH-level design work; a passing test suite is not treated as proof that the
interactive contract is complete.

### First implementation pass

The following concrete defects have been addressed in the current working
tree:

- Session event connections reject duplicate and non-contiguous sequences and
  request a baseline reopen when the cursor expires. The client does not
  silently continue from a broken replay cursor.
- Older-history responses carry the cursor they were requested for. A stale
  page is discarded instead of being prepended over a newer live projection.
- Composer input history supports ArrowUp/ArrowDown independently from server
  conversation history.
- Permission requests upsert their tool row and permission responses update
  that row's state, so a delayed result cannot leave an approval request
  invisible or permanently indistinguishable from a running tool.
- Compact lifecycle notifications are projected as runtime entries and the
  durable trajectory page restores them after a cold reopen.
- Agent task updates refresh child-thread summaries when the event identifies
  a child thread.
- Baseline reconciliation is serialized per active session. A cursor gap,
  expired cursor, and a concurrent older-page failure cannot start competing
  `openSession` calls; deleting the active session also clears the reconciliation
  target. Navigation now stops the previous stream before a new session/thread
  load, so a stale stream cannot reconcile the wrong session while navigation is
  in progress.

The following are explicitly **not** claimed as complete: a DSH-style
definition registry and live buffer, full subagent session navigation, or a
live-reload plugin settings system. A server-backed plugin settings surface
now exists below and covers declared XCore schemas at global, workspace, and
session layers.

### Schema-driven plugin settings

The server now exposes a revisioned `PluginConfigCatalog` and PATCH endpoint.
It discovers entries from the actual loaded plugin tree, projects producer
schemas to JSON Schema, and marks plugins without a declared `Config` as
read-only. The WebUI uses one schema-driven field editor with an advanced JSON
fallback and never branches on a plugin name. Writes are atomic,
optimistic-concurrency checked. Global/workspace writes affect later starts;
session writes are persisted for the selected session and are applied on its
next runtime creation rather than pretending to hot-reload a running plugin.

Remaining work is deliberate: provide a separate secret/write-only contract
and surface provider configuration only when its producer exposes a validated
schema. External plugins use the existing workspace `plugins.yaml` plus the
Python import environment; this catalog does not add another loader. Plugin-
local parser rules that are not represented by the XCore schema also need to
be reported by the server instead of guessed in the client.
The implementation must not add an event for every context-builder execution.

### Unified activity projection, first step

Turn lifecycle events (`turn_started`, `turn_finished`, and
`turn_cancelled`) now enter the same client timeline as user/assistant/tool
entries. Compact lifecycle entries use the same runtime-node path, while
persisted runtime messages continue to render their producer and event
provenance. This is a client projection of existing events only; it is not a
new persistence protocol and it does not make context-builder internals
visible. Live source-tagged `message` events now use the same runtime projection
as replayed history and retain a stable event-derived identity, so a reconnect
or duplicate frame cannot turn one injected context record into a user message
or duplicate node. The server `MessageData` contract accepts the optional
runtime provenance without changing ordinary user-message payloads, and claimed
non-user inbox inputs now populate that provenance on the live message event.
Lifecycle runtime nodes use `event:<sequence>:<type>` identities, while
source-tagged messages use `runtime:<message-id>`. The live claimed-message path now reuses the
session runtime's `_message_event` builder for ordinary and source-tagged
messages, including typed image/artifact serialization; it does not maintain a
second payload assembly path. A `history_updated` operation beginning with
`compact:` now re-adds a compact marker after the authoritative history
replacement, so the marker is not lost when the live stream reports the
compaction result.

Lifecycle nodes are now visible status rows instead of empty disclosure
controls. In the WebUI a user can immediately see when a turn starts, ends, is
cancelled, or when history compaction completes. Long context payloads remain
expandable because they are secondary detail. The UI still has no dedicated
trajectory tab.

### Durable trajectory page

The append-only `messages.jsonl` now has a typed, cursor-paginated read path
through persistence, `SessionsPort`, HTTP, the Python SDK, and the Web client.
It exposes ordinary messages, deterministic surface replacements, and log-only
events without exposing the persistence JSON codec. Appending records does not
invalidate an older trajectory cursor.

On session open the Web client loads the latest trajectory page and derives the
visible stream from it. Non-user inputs retain their source and appear as
context rather than user chat. Compaction start, summary, replacement, and end
records with the same `compaction_id` collapse into one durable row; the
original human transcript stays visible for transcript-preserving compaction.
Undo and clear replacements are folded as destructive transcript edits. Older
pages are accepted only for the cursor that requested them and the combined
window is reprojected, so delayed pages cannot overwrite a newer baseline.

Live SSE frames now enter a raw event window while the durable trajectory
baseline is being fetched. The baseline reducer folds that exact buffered
window once, then retries when the observed sequence advances; navigation drops
the old window before attaching a new thread. The reducer also keeps an
explicit live/trajectory origin, so a baseline replacement cannot erase frames
already rendered while the request was in flight. This is a transport
reconciliation buffer, not a new persistence format or per-context-build event
source; no per-context-build events were added.

Final assistant messages now carry a deterministic thread-local message ID,
which is persisted with the message and exposed by the trajectory record (not
by the older message-page contract). User inputs retain their inbox ID and Tool
records retain their call ID. The Web reducer uses those IDs to suppress a
frame replayed after its durable counterpart was already loaded; this closes
the common reconnect duplication without comparing message text.

### Subagent thread navigation

Persisted child threads already share the session thread catalog. The sidebar
now presents them as named subagent rows with their thread identity and running
indicator. The session page also exposes a DSH-style resident activity strip
for the complete thread summary, including running/idle state and model, and
selecting one opens that child through the normal thread API so it gets its own
trajectory, event stream, usage, and header state. Desktop and mobile browser
coverage exercises the actual navigation and child history.

This is not yet DSH's resident multi-session cluster: switching threads stops
the previous Web stream, and the sidebar does not aggregate recursive child
usage, elapsed time, or diagnostics into a separate subagent catalog. Those are
remaining enhancements rather than hidden behavior.

### Server settings, first connected surface

The Web settings dialog no longer presents a fake server preview. With an
active session it uses the same schema-described, revision-checked catalog for
global, workspace, and session scopes. Sandbox and permissions are plugin
declarations in that catalog; the typed `/policy` endpoint remains available
as a command/API projection. Provider secrets still require write-only slots
before they can be safely exposed, and writes do not pretend to hot-reload
running plugins.

History-changing server commands now refresh this trajectory projection rather
than falling back to the older message page. Concurrent refresh requests are
coalesced only while a request is active and then run once more, rather than
silently losing the later request.

### Evidence collected

- `npm test -- --run` in `XBotv2/web`: **90 tests passed in 21 files**.
- `npm run build` in `XBotv2/web`: TypeScript and Vite production build passed.
- The Playwright mock suite completed with **71 passed and 1 skipped** (72
  tests; the skip is an existing environment-dependent case).
- The full integration suite completed with **140 passed** when socket tests were run
  with the required host permission. The sandbox-only run reported socket
  `PermissionError` failures and is not treated as application evidence.
- Focused Python protocol/session event checks passed (**28 tests**). A later
  combined run also exposed two environment/repository conditions that are
  recorded rather than hidden: the current checkout lacks
  `docs/api/api_inventory.md`, and socket-backed integration tests require the
  host permission used by the earlier 88-test run.
- The full core suite completed with **717 passed** after removing the stale
  test dependency on the deleted `docs/api/api_inventory.md`. Public exports
  remain checked for uniqueness and resolvability; API behavior is covered by
  typed contract and OpenAPI assertions instead of a duplicated Markdown list.
- The requested llama.cpp host was probed without a connection on the usual
  ports 8080, 8000, and 1234. No real-provider result is claimed; the
  conclusions below use source inspection, existing mock tests, and protocol
  reasoning.

### Confirmed defects and high-risk paths

The body of each item preserves the evidence recorded during the original
audit. The heading is the current status; implemented behavior and remaining
limits are described in the sections above.

1. **Resolved: session event recovery could leave the UI stale.**
   `web/src/client/SessionEventConnection.ts` retries transport failures but
   terminates permanently on `session_event_cursor_expired`. It does not
   rebuild the session baseline or reopen the stream, unlike
   `WorkspaceEventConnection`, which calls `onResetRequired()` and refreshes
   both catalogs. The server stream is bounded (`SessionEventStream` and its
   subscriber queue are capacity 512); a slow tab or a long event burst can
   detach a subscriber and surface cursor expiry. There is also no sequence
   gap check in the Web client. This explains “refresh fixes it”, and can lose
   the latest tool/result/usage event from the visible projection until a
   manual resume.

2. **Resolved for durable pages: history pagination was not revision-aware.**
   `useXBot.loadEarlier()` captures one old cursor and blindly prepends the
   returned page. `runtimeReducer.history_prepend` performs no message/node
   identity de-duplication and has no relation to the active event sequence.
   A concurrent append, clear/undo/regenerate, or compaction changes the
   server history revision; the next page can then be rejected as an invalid
   cursor, overlap the current projection, or be applied beside a newer
   live projection. `history_updated` replaces the entire visible list and
   resets the cursor, so already-loaded older pages disappear after a command
   or mutation. This is a correctness issue, not merely a loading animation
   issue; the acceptance test must cover append + page, mutation + page, and
   reconnect during page load.

3. **Partially resolved: steering has a real step-boundary latency window.**
   The Web client decides `queue` vs `steer` from its local `turnRunning` and
   outstanding POST map (`web/src/state/useXBot.ts`). The server puts steering
   input in the `next-step` inbox and only claims it at an engine step
   boundary; `_request_wakeup()` deliberately does not preempt a locked turn.
   The POST response is drained but not used for rendering, while the
   resumable session stream is authoritative. If the stream is delayed,
   disconnected, or the local running flag is stale, the user sees a delayed
   or apparently missing steer even though it is durable in the inbox. The
   fix must define and display accepted/claimed/consumed phases rather than
   masking this with another poll loop.

4. **Resolved for persisted facts: runtime/event history was not one semantic timeline.**
   `runtimeReducer.applyEvent` has no cases for `compaction_started`,
   `compaction_completed`, or `compaction_failed`, although the compact plugin
   publishes all three and the TUI consumes them. Unknown events are silently
   ignored. Context building emits internal XCore events
   (`before/context`, `after/context`, `after/context-build`) but no Web
   projection; only persisted runtime user messages become
   `ContextInjectionRow`s. Consequently user input, assistant output, tool
   calls/results, context injection, and compaction cannot be inspected as one
   ordered conceptual stream in WebUI. This is a missing event contract, not
   a React rendering omission.

5. **Resolved: permission events did not reconcile the Web Tool row.**
   `permission_request` only appends an interaction dialog in
   `runtimeReducer`; it does not upsert the supplied `tool_call` or mark an
   existing call as “pending approval”. `permission_response_recorded` removes
   the dialog, and `permission_denied` adds a notice, but neither changes the
   tool entry status. The TUI does this association by request id. If the
   subsequent `tool_result` is delayed or missed by the session stream, WebUI
   shows a tool as pending/running forever. This is a concrete explanation for
   the reported “tool still running although thinking/text already arrived”.

6. **Resolved: Tool results were present but hidden by default
   (medium).**
   `tool_result` is handled and the existing unit/E2E mocks prove that result,
   data, errors, artifacts, and bounded output can render. The result body is
   inside a collapsed `<details>` element and the summary only shows a short
   argument/status preview. Thus a normal user can reasonably report “no
   result” even when the event arrived. This is a presentation/affordance
   gap; do not “fix” it by adding a second transport or duplicating results.

7. **Resolved: the Composer had no submitted-input history.**
   `Composer.tsx` handles ArrowUp/ArrowDown only for command suggestions. The
   application stores no sent-input ring and no key path loads prior user
   submissions. Pressing Up after a message therefore cannot recall the last
   input; this is independent of persisted conversation history.

8. **Partially resolved: subagents are navigable child threads, not a DSH-style
   concurrent session surface (confirmed design gap).**
   The server persists subagent threads and `/sessions/{id}/threads` returns
   them. WebUI renders them only under the currently selected session and
   refreshes the thread list indirectly after an `agent` task event. There is
   no cross-thread live event aggregation, subagent-specific title/status
   surface, or independent open/close/resume view while the parent remains
   visible. A missed `task_updated`/cursor recovery leaves a newly spawned
   thread absent until refresh or re-open. The implementation should align
   with DSH’s explicit subagent/session navigation rather than inventing a
   second session store.

9. **Partially resolved: server settings and plugin configuration are now
   connected.**
   `SettingsDialog.ServerSettings` edits the typed session policy and also
   consumes the revisioned plugin-config catalog. The catalog projects loaded
   producer schemas, effective/layer values, and validation errors through
   HTTP; global and workspace writes are atomic and optimistic-concurrency
   checked. It deliberately does not hot-reload running sessions. External
   plugin directories, session-local plugin-config writes, and write-only
   provider secret slots remain open design work.

10. **Resolved or recorded: smaller correctness/maintainability signals.**
    `session/protocol.py::_open_session_response` contains a duplicate history
    assignment. It is currently harmless, but it indicates the transport
    boundary needs a focused audit before pagination changes. More broadly,
    the Web reducer silently ignores unknown event types, and
    `SessionEventConnection` reports `onConnection(true)` before the first
    frame is received; both can make status indicators more optimistic than
    the actual stream.

### Priority and acceptance gates for the next implementation phase

1. Add a raw live buffer around baseline replacement so frames received during
   trajectory loading are folded once without a transient reset.
2. Evolve child-thread navigation into a resident session cluster with
   recursive subagent usage, elapsed time, diagnostics, and background updates.
3. Extend the revisioned plugin-config catalog with a session-local layer
   where appropriate and write-only secret slots. Keep controls generated from
   producer-owned schemas.
4. Make steering acceptance, claim, and consumption phases explicit in the UI
   without changing the Agent loop's step-boundary semantics.

The status labels above reflect implemented behavior and focused acceptance
paths, not the existence of green tests alone.

## 1. API Inventory And Behavior Gate

- Keep `api.__all__`, `api_inventory.md`, and public API tests aligned.
- Add signature/serialization checks when a public type becomes part of plugin
  examples or built-in plugin templates.
- Reject new built-in plugin imports from runtime internals.

## 2. C/S Protocol Unification

- HTTP JSON plus SSE `ServerEvent` is now the only active C/S transport model;
  the parallel JSONL frame model and compatibility event alias were removed.
- Server and client use the shared `protocol.sse` codec, and fixtures
  cover every current `ServerEventType`.
- Extend the current `ServerEvent` envelope into one DTO family for
  server-to-client events.
- Message `request_id` now flows through HTTP submission, `Engine.run_turn`,
  every turn-scoped Hook, and all SSE envelopes. Interaction payload ids remain
  distinct from the outer turn correlation id.
- Keep HTTP/SSE as the main transport path until alternate transports share the
  same event contract.
- Add producer-driven integration scenarios for tool call, permission, usage,
  interrupt, and error payloads beyond the complete envelope fixture inventory.
- Treat agent-initiated permission and user-input requests as first-class C/S
  capabilities. The server now registers each request before publishing its
  SSE event, and both response paths share the same coordination invariant.
- Permission requests, user-input requests, recorded responses, and their two
  HTTP request bodies now have typed DTOs while retaining the single
  `ServerEvent` envelope.
- Error, tool-result, and flat usage events now have typed payload DTOs. HTTP
  exception handlers share `ErrorResponse` serialization.
- Every current `ServerEventType` now has a typed payload DTO, and tests require
  `TYPED_SERVER_EVENT_TYPES` to cover the complete known event inventory.
- Unexpected engine exceptions now use `engine_error` with
  `details.exception_type`; keep the documented server-owned code inventory in
  sync when behavior changes.
- Started turns now close after engine errors with `error` followed by
  `turn_finished`. `TerminalSession` consumes the transport-only `end` sentinel,
  and `TuiState` is the single owner of domain state transitions.
- Interaction ids are treated as opaque correlation keys; the TUI associates
  permission acknowledgements with request state instead of parsing id
  prefixes. The HTTP turn bridge explicitly closes its Engine stream and
  cancels it when the SSE consumer disconnects.
- Real-socket tests cover both `ask_user` and permission response round trips,
  including responses that outlive the transport's ordinary read timeout.
- Keep live interaction waits runtime-owned and correlated by request id.
  Detaching one event client does not destroy the runtime; explicit close does.
  An inactive runtime can later be reconstructed from persisted message history.
- Persisted message history is restored into subsequent provider requests.
  Provider-request tests and a real Minimax TUI process restart verify this
  separately from the deliberately unsupported in-flight interaction recovery.

### Command Discovery And Dispatch

- Keep the server authoritative for command discovery. Session discovery
  combines built-in and plugin-owned human commands without exposing the Tool
  registry as a command inventory.
- Keep client-only commands local: the client may intercept commands such as
  exit or visual transcript clearing, but it must query all server capabilities
  instead of maintaining a parallel server-command inventory.
- Execute only explicit server commands through the command endpoint. Prompt
  commands expand deterministically before an Agent turn. Ordinary model Tools
  and MCP Tools are not slash-command entries.
- Keep Goal free of protocol-specific adapters. `/goal` is a plugin-owned human
  command; its Agent Tools use the normal Tool guard and result pipeline.
- Add contract tests proving server-command execution, prompt expansion,
  exclusion of ordinary Tool/MCP registrations, and client interception of a
  local command.

## 3. Runtime Event Contract

- Runtime extension points are named, owner-exported events dispatched on the
  XCore context: `ctx.serial` for short-circuit events and `ctx.emit` for
  observer events. Payloads use the owning plugin's public types.
- Public immutable `ContextComponent` values back
  `CONTEXT_COMPONENTS_BUILT`; listeners may replace the typed list, and
  invalid entries fail before provider conversion.
- Keep caller-level contract tests for message, tool, and permission event
  families. `BEFORE_TOOLS` exposes parsed `tool_calls` and the originating
  `agent_response` directly.
- `ConversationHistory` owns append and replace operations. Its persistence
  sink commits the current effective history before the in-memory projection
  changes; runtime events do not double as persistence checkpoints.
- Move direct runtime access out of event payloads only after equivalent
  plugin capabilities exist.
- Engine turn orchestration delegates message admission, context building,
  model-request preparation, tool batches, and finish behavior to explicit
  stage methods. These methods retain stage-specific return contracts and do
  not introduce a universal event result interpreter.

## 4. Plugin Lifecycle Model

- Setup and runtime registrations share one fiber ownership record; duplicate
  Tool keys fail before mutation, and disposal removes owned effects even when
  a plugin cleanup callback fails.
- Plugin setup is the component's `apply()` lifecycle. XCore fibers own its
  registered services, listeners, Tools, commands, and disposers; failed
  activation and application destruction unwind those effects in reverse
  ownership order without a second XBot lifecycle API.
- Normal session close destroys the mounted application once. Conversation
  mutations are already durable at their owning History/State/Inbox boundary,
  so close does not run a second persistence flush.
- Manifest `config_schema` and configured values now use Draft 2020-12
  validation before plugin import.
- Recoverable plugin state uses the shared `StateService` and a plugin
  namespace. Configuration remains startup input and is never a state file.
- Runtime/dynamic tool registrations are tracked by the plugin and
  unregistered in its disposer so unload and rollback remain complete.
- MCP initialization is idempotent and transactional per server. Optional
  failures roll back that server; required failures roll back the complete init
  attempt; session close removes tools and permits a fresh initialization.
- MCP transports now share a required initialize/initialized handshake. Tool
  schemas survive registration, call data survives normalization, and `isError`
  maps to a structured failure. Stdio requests are serialized and response ids
  are checked.
- Treat the current MCP integration as tools-only. Adopt a maintained SDK or
  design a complete Streamable HTTP/session layer before adding resources,
  prompts, server requests, subscriptions, or HTTP SSE.
- Skills discovery is idempotent per loaded session and rolls back every dynamic
  tool from a partial registration attempt.
- Keep built-in plugins using only the public API.

## 5. Tool System Contract

- Canonical registered names are stored on registry entries and exposed through
  command discovery.
- Provider-visible tool names are unique across namespaces, because model tool
  calls do not carry registry namespaces.
- Command discovery exposes registered-name metadata without changing existing
  string selectors.
- Manifest and runtime registration accept only execution metadata enforced by
  the dispatcher; Hook declarations accept exactly the current stage inventory.
- Built-in tools return `ToolResult`; structured data, errors, artifacts, and
  client events retain their fields through runtime conversion.
- Typed `ToolResult.data`, `error`, and `artifacts` now survive runtime
  conversion and appear on `tool_result` events.
- Large tool results use session-relative `artifacts/` paths through a
  read-only virtual mount. Keep logical paths independent of backing locations.
- Unimplemented `execution_mode` and `lock_fields` registration metadata were
  removed. Define batch scheduling, stable output ordering, Hook concurrency,
  live-interaction serialization, and lock semantics before adding a parallel
  API.
- Keep the core built-in tool set small and dependable.
- Add a configured maximum number of model/tool rounds per user turn. Repeated
  invalid tool calls must terminate with a structured error instead of allowing
  an unbounded provider retry loop. Cover the limit with persistence and final
  turn-event tests.
- Maintain a provider-compatibility matrix for tool JSON Schema features.
  Minimax's Anthropic-compatible endpoint did not reliably preserve arguments
  with a `oneOf`/`const` Goal schema, so built-ins must not depend on advanced
  schema keywords until real-provider contract tests pass.

## 6. Built-in Plugin Templates

- `PluginBase` has optional lifecycle defaults, and the built-ins now expose
  consistent cleanup and diagnostics behavior documented in `plugins.md`.
- Token Manager reads explicit Hook fields, uses collector methods for its own
  statistics, and resets plugin-owned memory on unload.
- Keep Skills as the template for prompt/tool capability plugins.
- Keep MCP as the template for external tool provider plugins.
- Keep token manager as the template for policy/observation plugins.
- Keep each built-in plugin documented as an example of the lifecycle model.

## 7. Built-in Workflow Plugins

Implement these as public-API consumers and reference plugins, in this order:

### Compact

- The initial plugin supports a model-visible request tool and automatic
  character-threshold invocation through `BEFORE_CONTEXT`.
- It compacts only before a user-message boundary and preserves recent complete
  turns, including tool calls, results, and the current request.
- Engine-owned atomic persistence makes resume reconstruct the same summary and
  recent tail. Failed auxiliary calls leave the original history intact.
- Automatic compaction has been verified with a real provider. A shared
  token-budget trigger remains; do not duplicate provider tokenizers inside
  the plugin or add another Hook stage.

### Todo List

- The plugin provides one atomic `update_todos` Tool; every call supplies the
  complete ordered checklist instead of per-item CRUD operations.
- A versioned `TodoSnapshot` in the plugin's shared `StateService` namespace
  makes each changed list one immediate persisted replacement. Resume retains
  the current active items.
- Todo calls and results remain on the normal conversation path so the next
  model call sees the update confirmation. The plugin does not repeatedly
  inject the active list. ToolResult carries a typed current-snapshot
  projection used consistently by persisted history, WebUI, and TUI; HTTP
  close/resume and completion clearing are covered with MockLLM.

### Goal

- `/goal` owns human lifecycle control. Agent-facing `create_goal`, `get_goal`,
  and `update_goal` use structured schemas and the normal Tool runtime.
- A versioned `GoalSnapshot` in the Goal namespace retains objective, status,
  summary, and optional token budget. Only continuation turns replace their
  accepted input with the active Goal context; terminal state does not inject
  context into unrelated turns.
- Todo items remain concrete work tracking. Active Goal continuation uses the
  runtime-only continuation; ESC pauses it and resume does not restore it. Real-provider tool selection, internal permission baseline,
  restart recovery, context injection, and terminal retention are verified.
- Connect an explicitly requested Goal `token_budget` to provider-reported
  usage. `/goal` must distinguish declared, used, and remaining tokens before
  any automatic pause or budget-exhaustion behavior is claimed.

Each plugin needs lifecycle rollback/unload tests, persistence and resume tests,
structured tool-result tests, public API boundary tests, and current
documentation before it becomes a shipped default.

## 8. Documentation As Implementation

- Keep examples runnable against current CLI and protocol behavior.
- Link each architecture claim to a typed model, test, or concrete file.
- Remove stale phase-plan claims once their replacement documentation exists.
- The unused planning-specific `dag_suffix` fragment name was replaced by the
  domain-neutral `context_suffix`. `PromptFragmentStage` now enumerates all
  supported values, manifests validate them before setup, and the old name is
  rejected rather than retained as a permanent alias.

## 9. TUI Semantic Views

- Keep Textual while it satisfies measurable input, rendering, and performance
  requirements. The HTTP/SSE transport remains the replacement boundary if a
  second client is evaluated later.
- Replace log-like presentation incrementally with semantic controls. Reasoning
  and tool details now collapse while their summary and live status remain
  visible. Full arguments, results, structured errors, data, and artifacts stay
  available inside Details. Client-owned `/thinking` and `/details` commands
  control current and future blocks without entering Agent history.
- Render assistant Markdown and fenced code consistently during streaming and
  resume while keeping user, reasoning, and tool payloads literal.
- Keep the bottom status line driven by open-session metadata and usage events.
  It now prioritizes run state, queue depth, token usage, current context
  remaining, workspace, model, and provider, with session identifiers shown
  only on wide terminals. Plan progress still requires authoritative runtime
  data.
- Background shell and subagent tasks expose stable IDs and authoritative
  lifecycle snapshots through `task_updated`; the TUI updates one collapsible
  Tasks control in place and distinguishes their `kind` without parsing text.
- Queued follow-ups now render ordered summaries beside Tasks in one runtime
  band. Their display lifecycle reuses the existing client request map while
  acceptance and fold ordering remain owned by the session.
- Narrow-terminal, long-transcript, semantic-control, and task-panel rendering
  have headless screenshot or layout coverage. Visual polish remains secondary
  to interaction behavior.
- Consider a parallel TypeScript/Ink prototype only after a reproducible
  Textual limitation or an independent Node client distribution requirement is
  documented. Do not replace the working client with an unverified rewrite.

## Termux / Android Platform Bugs

- **SSE `ServerEvent` validation crash (request_id missing)** — reproduced on
  Termux (Python 3.14 + pydantic 2.13.4) and latent on all platforms: the
  `request_permission` tool publishes a `permission_request` client event
  without a `request_id` (the permission guard already minted
  `permission:<tool_call.id>`), the live sink forwarded it unchanged, and the
  wire model (`PermissionRequestData.request_id` required) failed the SSE
  stream.  Fixed in the producer (`permissions/tools.py`: the tool now mints
  its own `request_id`), keeping the event contract with the producer that
  owns it (regression test:
  `test_request_permission_tool_emits_request_id`).
- **sand_messsage** tool missing 'source' key in sending events
