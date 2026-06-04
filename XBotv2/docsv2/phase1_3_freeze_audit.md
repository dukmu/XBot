# Phase 1-3 Freeze Audit

## Current Status

Phase 1-3 have a working core foundation, plugin loader, protocol frames/server,
non-curses terminal wrapper, and protocol-driven curses TUI shell. Core tests
are the primary freeze gate.

## Fixed Before Freeze

- Removed the placeholder core `ask` tool. Core now registers event-driven
  `send_message` and `ask_user` tools instead: `send_message` emits a
  non-blocking `client_message`, while `ask_user` emits `user_input_required`,
  marks the session interrupted, and stops the current turn until a future
  resume protocol is implemented.
- Expanded filesystem tools:
  - `filesystem_read` returns JSON content plus path, size, mtime, line count,
    returned line count, and truncation flags.
  - `filesystem_list` returns JSON directory and entry metadata.
  - `filesystem_write` supports overwrite, append, prepend, insert line,
    replace lines, regex replacement, and single-file unified diff patch modes.
- Sandboxed tool execution resolves path-like arguments to the workspace before
  invoking the tool.
- Personality tool selectors now call `ToolRegistry.restrict()`, so the model
  and runtime see only enabled tools. Unknown selectors fail closed instead of
  silently exposing all registered tools.
- Permission and sandbox `ask` decisions now emit protocol-visible
  `permission_request` events and fail closed until protocol/TUI interactive
  approval exists; they are no longer treated as implicit allow. Denials emit
  `permission_denied`.
- Added a default `AFTER_TOOLS` hook that caches oversized tool results under
  `state/artifacts/tool_results/` and replaces inline results with a bounded
  preview before history persistence and JSONL emission.
- Plugin prompt fragment files now resolve relative to the discovered plugin
  directory for both `PluginBase` subclasses and manifest-only default plugins.
- Plugin discovery/loading now lives in `xbotv2.plugin.loader.PluginLoader`
  instead of being hidden inside bootstrap, and core tests cover direct loader
  discovery plus prompt fragment registration.
- Plugin unloading now has a real cleanup path: `PluginLoader.unload()` calls
  `on_unload()`, unregisters recorded hooks, tools, and prompt fragments, and
  releases temporary import paths when no plugins remain loaded.
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
- Bootstrap now passes externally supplied `plugin_configs` through to
  `PluginLoader`, while preserving personality plugin config overrides.
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
- `test_protocol.py` now launches `python -m xbotv2 --mode server` as a real
  JSONL stdio subprocess and verifies `hello`, `session.open`, `user.message`,
  and `shutdown` frame flow with stable session/thread IDs.
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
- TUI state now preserves approval-required, permission-denied,
  waiting-for-user, and error statuses across the following `turn_finished`
  frame, matching the materialized interruption/error semantics instead of
  briefly showing a misleading ready state.
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

- Permission and sandbox `ask` decisions still do not resume a turn through
  JSONL/TUI after approval. The current runtime emits the request event and
  fails closed; resume is the remaining feature gap.
- Phase 4 built-in plugins are still empty directories, so Phase 1-3 freeze
  should be judged only as a plugin-capable core, not as migrated feature
  parity.
- Token estimation, token usage statistics, and token budget control are not
  implemented modules yet. The core now exposes the needed hook surface and
  source-tagged context metadata, but no plugin consumes it yet.
- The subprocess tests cover direct server JSONL, interaction event streaming,
  and non-curses terminal wrapper roundtrips. Curses screen-level behavior is
  covered only by state/queue/import/render smoke tests, not by an interactive
  terminal golden test.

## Freeze Gates

Run from repository root:

```bash
uv run pytest XBotv2/tests/core/ -q
python -m py_compile \
  XBotv2/xbotv2/core/bootstrap.py \
  XBotv2/xbotv2/core/engine.py \
  XBotv2/xbotv2/core/builtin_tools/filesystem.py \
  XBotv2/xbotv2/core/builtin_tools/interaction.py \
  XBotv2/xbotv2/tools/runtime.py \
  XBotv2/xbotv2/tools/result_cache.py \
  XBotv2/xbotv2/plugin/manifest.py \
  XBotv2/xbotv2/plugin/base.py \
  XBotv2/xbotv2/plugin/loader.py \
  XBotv2/xbotv2/persistence/materializer.py \
  XBotv2/xbotv2/persistence/store.py \
  XBotv2/xbotv2/tui/client.py \
  XBotv2/xbotv2/tui/terminal.py
```
