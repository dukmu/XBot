# Storage and plugin closure evidence

This audit records the production owners and observable closure evidence for
the storage and capability-plugin boundaries. It is deliberately narrower than
a feature roadmap: unsupported multi-writer storage, new indexes, garbage
collection, and compatibility readers are not implied.

## Storage boundaries

| Scope | Owner and path | Production evidence | Closure |
| --- | --- | --- | --- |
| Conversation trace | `MessageHistoryStore` writes the versioned `StoredTrajectoryRecord` union to `messages.jsonl`; `_TrajectoryState` alone parses and folds it through `_SurfaceState` and `_TranscriptState`. | Core persistence tests cover append and replacement failure, torn tails, complete corrupt records, external suffixes, projection identity/source validation, pagination anchors, and transaction recovery. Cold parsing now validates both projections once before publishing the record cache; warm appends fold only their suffix. | One canonical JSONL format and one fold implementation. Cold work is O(records); subsequent pages and reads reuse the bounded cache. Rewriting a committed prefix remains outside the single-writer append-only contract; same-size rewrites are not promised to be detected. |
| Inbox | `InboxStore` writes only `StoredInboxRecord` mutations to `inbox.jsonl`; `AgentInbox` owns transient claims. | Growth tests account for bytes written, mutation replay, commit reconciliation, torn-tail removal, append rollback, and close/resume behavior. | Append-only input recovery is separate from conversation replay. No snapshot compatibility path or duplicate commit ledger exists. |
| Metadata | `ThreadMetadataState` publishes initialization/change facts; the persistence subscriber writes `thread.json` with `write_text_atomic`. | Startup tests prove one initial write, read-only hydration on resume in either plugin order, deferred materialization, and thread-local restored state. | Atomic snapshot, not a log. Hydration does not rewrite the file it read. A write failure propagates; the last successfully replaced file remains authoritative on restart. |
| Plugin state and usage | XCore `StateService` owns one atomic `plugin_state/state.json`; `UsageService` owns the `usage/counters` value. | Usage tests prove exact counter addition, snapshot-only persistence, auxiliary-request accumulation without replacing latest turn context, and initialization ordering. HTTP close/resume tests prove counters and task state survive. | KV snapshot remains unchanged. Auxiliary caption, compaction, goal evaluation, and MCP sampling usage is intentionally cumulative and cannot be reconstructed from conversation messages alone. Each thread binds a separate file, so there is no process-global multi-session counter to reconcile. |
| Artifacts | Session-owned `ArtifactStore` writes content-addressed bytes atomically under `artifacts/<kind>/<digest>`. Content cache owns externalization; context compilation resolves logical references. | Content-cache tests prove complete UTF-8 retention, non-mutating projections, and no published reference when a write fails. Compaction tests resolve artifact-bearing history and continue after resume. | Persisted messages contain logical IDs, never active absolute paths. Atomic replacement prevents a failed write from publishing a partial payload; external disk corruption is not silently repaired. |
| Child lifecycle | `ThreadLifecycleStore` owns session-level `threads.jsonl`; child execution is its only writer. Version 2 is a strict started/completed/failed/cancelled union rather than one record with event-dependent empty fields. | Persistence tests inject a real partial write followed by failure and prove the prior prefix remains readable and retryable. Subagent production tests observe started plus the appropriate terminal variant. | The common append owner completes short writes, fsyncs success, and truncates a failed suffix before propagating the original error. Static parent/agent facts occur only on `started`; v1 is rejected without a compatibility path. |

The bounded trajectory cache keeps at most eight recent paths and 60,000
records, while a live store retains its own state. Rotating beyond the bound can
make an inactive path cold again; that is bounded-memory behavior, not repeated
prefix work for a live reader. The earlier 10/20/40-turn production measurement
showed linear history bytes and constant-size per-turn StateService snapshots;
this audit does not replace those measured results with timing claims.

### Complexity boundaries checked

- **Warm compaction:**
  `test_compaction_folds_only_the_new_record_on_warm_projections` exercises
  prefixes of 10 and 100 records and three nested compactions. The observed
  fold count stays at no more than six records: one new record for each of the
  surface and transcript projections per compaction. The prefix is neither
  reread nor cloned. Appended JSONL bytes are the new replacement record only.
- **Cache rotation and multiple paths:**
  `test_live_threads_append_and_page_without_rereading_their_prefix` keeps 12
  live thread paths, beyond the recent-reader limit of eight, for 20 and 40
  turns. Reads of known prefixes remain zero and measured write bytes equal the
  final trajectory sizes. A live store owns its `_TrajectoryState` even after
  recent-cache eviction. An inactive, unreferenced path may be evicted and its
  next open is one O(records) cold validation; it is then warm again.
- **Foreign append and paging:**
  `test_reader_folds_only_external_append_suffix` warms all three projections,
  then applies a compaction plus ten foreign appends. Measured read bytes equal
  exactly the file-size suffix after the original prefix. Paging does not
  reread that prefix.
- **Close and reopen:**
  close releases the live store; the bounded recent cache may still retain its
  state. Reopening after eviction performs one linear parse/fold.
  `test_automatic_compaction_is_triggered_before_request_and_survives_resume`
  verifies the compacted surface restores once, and
  `test_todo_and_usage_survive_http_close_resume` verifies the atomic task and
  usage snapshots restore without replay writes. This is cold O(records), not
  O(pages × records), because the reopened store caches the validated result.
- **Multiple sessions:** every cache key is the absolute `messages.jsonl` path,
  and every metadata, artifact, inbox, and StateService path is derived from
  `SessionPaths → ThreadPaths`. Sessions therefore share neither a trajectory
  projection nor usage counters. The 12-live-path byte test proves the
  path-local append behavior; changing the session component changes only the
  key prefix and not the algorithm. No process-global multi-session usage or
  compaction counter exists to merge or scan.

## Capability plugins

| Plugin | Public owner and observable paths | Persistence and feedback | Teardown or partial failure |
| --- | --- | --- | --- |
| Compact | `CompactService`, the `compact` tool, and `/compact` command own one transaction over `ConversationHistory`. | The service records durable start/end events, commits the surface replacement before publishing `HistoryChanged`, and records auxiliary model usage. Production tests cover manual, automatic, overflow-retry, artifact-bearing, cancellation, provider failure, resume, and later turns. | Fiber-owned tool/command/listener registrations unload automatically; `_dispose` clears in-flight service state. Every pre-commit failure preserves live and durable history, while open transaction recovery is explicit. |
| Goal | `GoalService` owns the `goal` StateService namespace, command/tools, evaluator tasks, and `GoalChanged` runtime event. | State is written before advisory client notification. Autonomous rounds are persisted `RuntimeInput` values targeted at `NEXT_TURN` with `wake=True`; post-compaction context is `NEXT_STEP` with `wake=False`. HTTP tests observe active/terminal events, exact cumulative usage including evaluator calls, todo stats, close/resume, interrupt, and failed verdict persistence. | Session close/dispose cancels evaluator and retry tasks. There is one active goal per thread; no cross-session scheduler state is claimed. |
| Todo list | `TaskService` owns the `todolist` StateService namespace, task tools, `TaskChanged`, and `GET_TODOS`. | A task snapshot is persisted before `TaskChanged`. Stale and compaction reminders enter the durable inbox as attributed `RuntimeInput` at `NEXT_STEP` with `wake=False`; tests prove one fold, counter reset, close/resume, and no duplicate compaction reminder. | Registrations and listeners are fiber-owned. The plugin has no background process or independent queue to unload. |
| Skills | `SkillsPlugin` discovers `SKILL.md`, registers scoped tools/commands on application initialization, and owns the per-turn permission scope. | Factory tests cover discovery and standard tool execution, including chaining another skill while an active skill still denies an ordinary tool outside its allowlist. Prompt integration proves user invocation expands before the model request. | Initialization records every caller-owned late registration and rolls them back on any partial failure; dispose unregisters them and clears active scopes. No persisted runtime handle exists. |
| MCP | `MCPPlugin` owns `MCPClient`, namespaced tools, protocol bridges, and server diagnostics. | A real local FastMCP stdio test runs through the application factory, list-tools discovery, model-authored standard tool dispatch, canonical result, and application destruction. | Per-server initialization failure unregisters its partial registrations and disconnects it; a required failure rolls back all servers. Session close and dispose remove tools before disconnecting all transports. The real child PID is observed gone after destroy. |
| Browser | `BrowserPlugin` owns lazy `WebAccess` and `BrowserSession`; tools use the standard registry and sandbox/permission guards. | Core production tests cover registered fetch/search, local HTTP, redirects, response bounds, DNS rebinding, file policy, real Chromium interaction, screenshots in ArtifactStore, permission denial/cancellation, and stale refs. | Dispose closes both lazy resources. Failure aggregation attempts all cleanup and reports the failure; real-browser tests verify Chromium closure. Before lazy construction there is no external resource to release. |

Goal/todo client events are projections, not a second state authority. Their
durable StateService snapshots and attributed inbox records reconstruct the
state after reconnect; neither plugin turns a status-only notification into an
extra user turn. Skills, MCP, and browser do not persist live registries,
transports, processes, pages, or permission scopes.

No raw historical user-feedback reproduction was available for the earlier
goal/todo concern, so this audit does **not** claim that an observed feedback
incident was reproduced and fixed. It closes the currently public chain only:
state changes are visible through `GoalChanged`/`TaskChanged`, durable state is
visible after close/resume, goal rounds intentionally wake autonomous work,
and todo/compaction reminders are persisted without waking a turn. A future
report with a missing or duplicate client event still needs its own event trace
and reproduction.

## Verification commands

All commands use the repository virtual environment and an absolute worktree
`PYTHONPATH` containing the worktree root, `XBotv2`, and `XCore`.

- Persistence/history: `python -m pytest XBotv2/tests/core/test_persistence.py XBotv2/tests/core/test_history_cursor_contract.py XBotv2/tests/core/test_history_identity.py -q` — 49 passed.
- Metadata/usage: `python -m pytest XBotv2/tests/core/test_usage.py XBotv2/tests/core/test_application_startup.py::test_metadata_initialization_is_durable_and_resume_is_read_only XBotv2/tests/core/test_application_startup.py::test_runtime_state_is_shared_restorable_and_thread_local -q` — 8 passed.
- Compaction: `python -m pytest XBotv2/tests/core/test_compact.py -q` — 28 passed.
- Goal/todo/skills: `python -m pytest XBotv2/tests/core/test_goal.py XBotv2/tests/core/test_todolist.py XBotv2/tests/core/test_skills.py -q` — 33 passed.
- HTTP goal/todo/skills production paths — 9 passed because one of these eight
  tests is parameterized:
  `test_todo_and_usage_survive_http_close_resume`,
  `test_http_goal_command_runs_the_evaluator_loop`,
  `test_goal_close_cancels_evaluator_and_resume_restarts_active_goal`,
  `test_goal_close_cancels_scheduled_retry_timer`,
  `test_terminal_goal_persists_usage_tool_and_todolist_stats`,
  `test_http_goal_impossible_verdict_persists_failed_state`,
  `test_http_goal_interrupt_pauses_and_persists_goal`, and
  `test_http_skill_prompt_is_expanded_before_model_input`.
- MCP: `python -m pytest XBotv2/tests/core/test_mcp.py -q` — 5 passed with a real stdio child.
- Browser: `python -m pytest XBotv2/tests/core/test_browser.py -q` — 36 passed with local sockets and installed Chromium.
- Artifacts/content cache: `python -m pytest XBotv2/tests/core/test_content_cache.py -q` — 10 passed.

The MCP and browser commands require normal local process/socket access. The
first combined sandboxed run was interrupted after those resource-dependent
tests failed or stalled; both suites passed when rerun with that access. No
external network service or credential was used.
