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
- Documentation now uses the implemented 17 hook stages, not the stale 18-stage
  wording.
- `CoreStateStore.materialize()` now delegates derived state construction to
  the planned `persistence.materializer` module, and core tests cover that pure
  function directly.
- `provider: mock` can now be configured through `provider.yaml`, enabling
  deterministic subprocess smoke tests without a real LLM provider.
- `test_protocol.py` now launches `python -m xbotv2 --mode server` as a real
  JSONL stdio subprocess and verifies `hello`, `session.open`, `user.message`,
  and `shutdown` frame flow with stable session/thread IDs.
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
