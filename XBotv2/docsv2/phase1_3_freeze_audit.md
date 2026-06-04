# Phase 1-3 Freeze Audit

## Current Status

Phase 1-3 have a working core foundation, plugin loader, protocol frames/server,
non-curses terminal wrapper, and protocol-driven curses TUI shell. Core tests
are the primary freeze gate.

## Fixed Before Freeze

- Removed the placeholder core `ask` tool. Core now registers event-driven
  `send_message` and `ask_user` tools instead: `send_message` emits a
  non-blocking `client_message`, while `ask_user` emits `user_input_required`,
  waits for a live `user.input` reply on the active protocol connection, and
  returns the answer as a tool result so the current ReAct turn can continue.
  The request advertises `resume_supported: false`: timeouts return a no-reply
  tool result, while client disconnect records cancellation and stops the
  current turn without durable resume.
- Interaction and permission request events now carry stable correlation
  metadata (`user_input:<tool_call_id>` and `permission:<tool_call_id>` request
  ids plus source/tool-call metadata). `state.yaml` materializes unresolved
  requests as `pending_interactions` from the append-only event log.
- JSONL protocol now accepts live `user.input` and `permission.response`
  commands, records `user_input_response` / `user_input_cancelled` /
  `permission_response` events, returns bounded `*_recorded`
  acknowledgements, clears matching `pending_interactions`, and stores the
  original pending request snapshot on the response event.
- Expanded filesystem tools:
  - `filesystem_read` returns JSON content plus path, size, mtime, line count,
    returned line count, and truncation flags.
  - `filesystem_list` returns JSON directory and entry metadata.
  - `filesystem_write` supports overwrite, append, prepend, insert line,
    replace lines, regex replacement, and single-file unified diff patch modes.
- Sandboxed tool execution resolves path-like arguments to the workspace before
  invoking the tool.
- Personality tool selectors now call `ToolRegistry.restrict()` after core and
  plugin tools are registered, so selectors can enable plugin tools and both
  the model and runtime see only enabled tools. Unknown selectors fail closed
  instead of silently exposing all registered tools.
- Plugin loader resource tracking now uses all registered tool names rather
  than the restricted visible view, so plugin unload removes hidden plugin
  tools reliably.
- Permission and sandbox `ask` decisions now emit protocol-visible
  `permission_request` events, wait for live `permission.response` decisions
  during active protocol turns, and continue the current tool call when allowed.
  Deny, timeout, disconnect, and non-live runtimes fail closed. Denials emit
  `permission_denied`.
- Sandbox one-call approvals now participate in guard evaluation and are
  consumed after one matching path/tool call instead of being inert bookkeeping.
- Added a default `AFTER_TOOLS` hook that caches oversized tool results under
  `state/artifacts/tool_results/` and replaces inline results with a bounded
  preview before history persistence and JSONL emission. Cache metadata is
  stored in both `tool_result_cached` events and the persisted ToolMessage
  artifact.
- Plugin prompt fragment files now resolve relative to the discovered plugin
  directory for both `PluginBase` subclasses and manifest-only default plugins.
- Plugin discovery/loading now lives in `xbotv2.plugin.loader.PluginLoader`
  instead of being hidden inside bootstrap, and core tests cover direct loader
  discovery plus prompt fragment registration.
- Plugin registration failures now roll back newly registered hooks, tools, and
  prompt fragments before re-raising the original load error; if `on_load()`
  already ran, `on_unload()` is called for plugin-local cleanup.
- `PluginLoader.load()` is now atomic across the load call: if a later plugin
  fails, plugins loaded earlier in that call are unloaded and their registered
  resources are removed before the error escapes.
- Plugin unloading now has a real cleanup path: `PluginLoader.unload()` calls
  `on_unload()`, unregisters recorded hooks, tools, and prompt fragments, and
  releases temporary import paths when no plugins remain loaded.
- Plugin load failures now also release loader-added import paths that are not
  needed by already loaded plugins, so broken plugins cannot pollute later
  imports in the same process.
- Plugin import setup now keeps external plugin roots importable while loaded
  and drops stale `sys.modules` entries when the same plugin name is loaded
  from a different directory, avoiding handler-resolution bugs during tests or
  future reload flows.
- Manifest-only plugin hook/tool handlers and prompt fragment files now fail
  loudly when a declaration cannot be resolved instead of silently skipping
  broken plugin configuration.
- Root pytest configuration now includes `pythonpath = ["XBotv2", "."]`, so
  XBotv2 tests run from repository root without manual `PYTHONPATH`.
- Personality-declared hooks in `personality.yaml` are now resolved and
  registered during bootstrap; invalid hook targets fail loudly instead of
  being silently ignored.
- Documentation now uses the implemented 42 hook stages, including user intake,
  source-tagged context component, pre-bind tool schema, provider error,
  stop/failure, pre/post compact, permission request/denied, per-tool-call,
  tool batch, client-event, and persistence hooks needed for future token
  estimation, statistics, and budget control plugins.
- Critical hook stages now fail visibly: `ON_SESSION_INIT`,
  `ON_SESSION_CLOSE`, `BEFORE_STATE_PERSIST`, and `AFTER_STATE_PERSIST` run all
  callbacks, then raise an `ExceptionGroup` if any hook failed. Observation
  stages such as `ON_TURN_START` continue to log-and-continue.
- Bootstrap now passes externally supplied `plugin_configs` through to
  `PluginLoader`, while preserving personality plugin config overrides.
- Bootstrap now treats `plugin_dirs=None` as the default built-in plugin scan
  and explicit `plugin_dirs=[]` as no-plugin mode, so Phase 1-3 core tests do
  not depend on Phase 4 built-in plugin directories staying manifest-free.
- CLI/server/TUI entrypoints now expose `--no-plugins`; protocol subprocess
  core tests use it so JSONL and terminal wrapper smoke coverage remains a
  pure-core freeze gate after built-in plugin manifests are added.
- Added `docsv2/token_budget_hooks.md` to record the current token-estimation
  gap, the fine-grained hook surface now available to plugins, and the evidence
  needed before token budget control can be frozen.
- Engine turn failures now append an `error` event, run `ON_ERROR` hooks with
  the raised exception in `HookContext.error`, emit an `error` protocol event,
  and materialize error status.
- `CoreStateStore.materialize()` now delegates derived state construction to
  the planned `persistence.materializer` module, and core tests cover that pure
  function directly.
- Materialized status now follows ordered event semantics: a later
  `turn_started` reactivates prior `error`/`interrupted` sessions, while
  `turn_finished` does not hide an interruption from the same turn.
- `CoreStateStore.truncate_messages(keep_last=0)` now returns the actual
  number of deleted messages, matching the method contract used by compaction.
- `provider: mock` can now be configured through `provider.yaml`, enabling
  deterministic subprocess smoke tests without a real LLM provider.
- `MockLLM` now records the actual request messages, stop values, kwargs, and
  normalized response tool calls for context/assertion-heavy plugin tests.
- Unknown provider names now fail closed with `ValueError` instead of silently
  falling back to OpenAI-compatible defaults.
- `test_protocol.py` now launches `python -m xbotv2 --mode server
  --no-plugins` as a real JSONL stdio subprocess and verifies `hello`,
  `session.open`, `user.message`, and `shutdown` frame flow with stable
  session/thread IDs.
- Protocol subprocess tests now also verify `send_message` and `ask_user`
  produce streamed `client_message` and `user_input_required` frames with the
  original `request_id`.
- Protocol encoder/server responses now preserve the client `request_id` in
  the frame envelope for command acknowledgements and all turn event frames,
  so TUI and external clients can correlate streamed responses.
- Protocol server input validation now returns bounded `error` frames for
  malformed JSON and empty `user.message` payloads instead of silently logging
  or leaving clients waiting.
- User-intake hook short-circuits now emit a bounded `user_message_rejected`
  error if the hook does not provide its own event, so protocol clients never
  hang on a pre-history rejection.
- Pre-context and pre-request guard hook short-circuits now fail closed with a
  bounded `hook_short_circuit_rejected` error when a hook returns a bare truthy
  value instead of a structured result; this prevents accidental provider calls
  after a plugin intended to stop execution.
- Per-tool `BEFORE_TOOL_CALL` rewrites now update the resulting
  `ToolMessage.tool_call_id` and re-run sandbox path resolution for rewritten
  calls, so tool lifecycle hooks cannot desynchronize protocol-visible IDs or
  execute rewritten paths outside the workspace.
- `POST_TOOL_BATCH` now receives the actual per-result tool calls after
  `BEFORE_TOOL_CALL` rewrites and sandbox path resolution, keeping batch-level
  auditing aligned with per-tool hooks and protocol-visible result IDs.
- Message persistence now round-trips standard LangChain metadata needed for
  deterministic resume and plugin audits: message id/name, AI tool calls,
  ToolMessage status/artifacts, public `additional_kwargs`, and provider
  `response_metadata`. Internal `xbotv2_` side-channel kwargs stay out of
  restored history because their events are persisted separately.
- Engine message saves now preserve `msg_id` and `ts` for unchanged retained
  history rows while still replacing the log from current in-memory history, so
  compaction does not cause avoidable audit identifier churn.
- TUI state now preserves approval-required, permission-denied,
  waiting-for-user, and error statuses across the following `turn_finished`
  frame, matching the materialized interruption/error semantics instead of
  briefly showing a misleading ready state.
- TUI state now also renders `user_input_recorded` and
  `permission_response_recorded` acknowledgements, returning to `Ready` after a
  response is accepted.
- Protocol `shutdown` now calls `Engine.close_session()` before returning
  `shutdown_ok`, so JSONL/TUI exits run `ON_SESSION_CLOSE`, append
  `session_closed`, save messages, and materialize closed state like direct CLI
  exits.
- `session_closed` now clears materialized `pending_interactions`, so a closed
  session cannot continue to advertise stale user-input or permission requests.
- `Engine.start_session()` now uses `CoreStateStore.has_existing_session()` so
  event-only sessions, such as a session that was opened and then closed before
  any user message, run `ON_SESSION_RESUME` instead of being misclassified as
  brand-new.
- Bootstrap now validates `personality_id`, `provider_name`, `session_id`, and
  `thread_id` before constructing config or session paths. Protocol
  `session.open` fails closed for path-like identifiers, preventing traversal
  outside the configured `sessions/` root.
- `test_protocol.py` also launches the non-curses `TerminalSession` wrapper
  against that server subprocess and verifies the client wrapper message
  roundtrip with a deterministic mock provider.
- Added `xbotv2/tui/client.py`, a protocol-driven curses TUI shell with
  replayable `TuiState`, background event queue draining, and a `--mode tui`
  CLI entrypoint. Core tests cover frame application, rendering, queue draining,
  interaction-event rendering, and the TUI/runtime dependency boundary.
- Added `docsv2/protocol.md` to document Phase 1-3 protocol events,
  interaction semantics, and client coverage.

## Remaining Weak Points

- Phase 4 built-in plugins are still empty directories, so Phase 1-3 freeze
  should be judged only as a plugin-capable core, not as migrated feature
  parity.
- Token estimation, token usage statistics, and token budget control are not
  implemented modules yet. The core now exposes the needed hook surface and
  source-tagged context metadata, but no plugin consumes it yet.
- The subprocess tests cover direct server JSONL, interaction event streaming,
  live user-input and permission responses, and non-curses terminal wrapper
  roundtrips. Curses behavior is covered by state/render, queue draining,
  dependency-boundary, and live interaction routing tests, but not by an
  interactive terminal golden test.

## Freeze Gates

Run from repository root:

```bash
uv run pytest XBotv2/tests/core/ -q
python -m compileall -q XBotv2/xbotv2
git diff --check
```
