# Phase 1-3 Freeze Audit

## Current Status

Phase 1-3 have a working core foundation, plugin loader, protocol frames/server,
non-curses terminal wrapper, and protocol-driven curses TUI shell. Core tests
are the primary freeze gate.

## Fixed Before Freeze

- Removed the placeholder core `ask` tool. It is not registered until the
  protocol and TUI support a real interrupt/resume interaction flow.
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
- Permission and sandbox `ask` decisions now fail closed until protocol/TUI
  interactive approval exists; they are no longer treated as implicit allow.
- Added a default `AFTER_TOOLS` hook that caches oversized tool results under
  `state/artifacts/tool_results/` and replaces inline results with a bounded
  preview before history persistence and JSONL emission.
- Plugin prompt fragment files now resolve relative to the discovered plugin
  directory for both `PluginBase` subclasses and manifest-only default plugins.
- Plugin discovery/loading now lives in `xbotv2.plugin.loader.PluginLoader`
  instead of being hidden inside bootstrap, and core tests cover direct loader
  discovery plus prompt fragment registration.
- Manifest-only plugin hook/tool handlers and prompt fragment files now fail
  loudly when a declaration cannot be resolved instead of silently skipping
  broken plugin configuration.
- Root pytest configuration now includes `pythonpath = ["XBotv2", "."]`, so
  XBotv2 tests run from repository root without manual `PYTHONPATH`.
- Personality-declared hooks in `personality.yaml` are now resolved and
  registered during bootstrap; invalid hook targets fail loudly instead of
  being silently ignored.
- Documentation now uses the implemented 33 hook stages, including user intake,
  source-tagged context component, pre-bind tool schema, provider error,
  per-tool-call, and persistence hooks needed for future token estimation,
  statistics, and budget control plugins.
- Added `docsv2/token_budget_hooks.md` to record the current token-estimation
  gap, the fine-grained hook surface now available to plugins, and the evidence
  needed before token budget control can be frozen.
- Engine turn failures now append an `error` event, run `ON_ERROR` hooks with
  the raised exception in `HookContext.error`, emit an `error` protocol event,
  and materialize error status.
- `CoreStateStore.materialize()` now delegates derived state construction to
  the planned `persistence.materializer` module, and core tests cover that pure
  function directly.
- `provider: mock` can now be configured through `provider.yaml`, enabling
  deterministic subprocess smoke tests without a real LLM provider.
- `test_protocol.py` now launches `python -m xbotv2 --mode server` as a real
  JSONL stdio subprocess and verifies `hello`, `session.open`, `user.message`,
  and `shutdown` frame flow with stable session/thread IDs.
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
- `test_protocol.py` also launches the non-curses `TerminalSession` wrapper
  against that server subprocess and verifies the client wrapper message
  roundtrip with a deterministic mock provider.
- Added `xbotv2/tui/client.py`, a protocol-driven curses TUI shell with
  replayable `TuiState`, background event queue draining, and a `--mode tui`
  CLI entrypoint. Core tests cover frame application, rendering, queue draining,
  and the TUI/runtime dependency boundary.

## Remaining Weak Points

- Permission and sandbox `ask` decisions still do not interrupt and resume a
  turn through JSONL/TUI. This is now a feature gap; the current runtime fails
  closed instead.
- Phase 4 built-in plugins are still empty directories, so Phase 1-3 freeze
  should be judged only as a plugin-capable core, not as migrated feature
  parity.
- Token estimation, token usage statistics, and token budget control are not
  implemented modules yet. The core now exposes the needed hook surface and
  source-tagged context metadata, but no plugin consumes it yet.
- The subprocess tests cover direct server JSONL and non-curses terminal wrapper
  roundtrips. Curses screen-level behavior is covered only by state/queue/import
  smoke tests, not by an interactive terminal golden test.

## Freeze Gates

Run from repository root:

```bash
uv run pytest XBotv2/tests/core/ -q
python -m py_compile \
  XBotv2/xbotv2/core/bootstrap.py \
  XBotv2/xbotv2/core/engine.py \
  XBotv2/xbotv2/core/builtin_tools/filesystem.py \
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
