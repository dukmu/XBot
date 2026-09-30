# Getting started

Use the interpreter that owns `xbot`:

```bash
uv add xbotv2
uv run xbot --help

# or
python -m pip install xbotv2
python -m xbot --help
```

For a checkout, use `.venv/bin/python`/`.venv/bin/xbot` and set
`PYTHONPATH=XBotv2` only when running checkout source. Always choose an
explicit data directory and workspace for tests:

```bash
xbot once --data-dir ./run-data --workspace ./workspace "List the files"
```

Interactive terminal use is `xbot tui` (also the default when no subcommand is
given). It starts a local API server unless `--server URL` points to an
existing one. `xbot serve` starts only the HTTP/SSE API; `xbot web` serves the
compiled Web client; `xbot acp` runs the ACP carrier. Use `xbot --help` and
`xbot <mode> --help` for the installed version's flags. TUI history paging and
retention flags and its client plugin settings are summarized in
[clients](clients.md).

An external plugin is a normal package. Its root `plugin.py` exports one
module-level `plugin` object; public declarations belong in `contracts.py`,
`events.py`, and `protocol.py`:

```text
weather-plugin/
├── pyproject.toml
├── src/weather_plugin/{__init__.py,contracts.py,events.py,protocol.py,plugin.py}
└── tests/
```

Register it through a data/workspace `plugins.yaml` overlay. The complete
XCore plugin lifecycle, plugin-owned Pydantic config models, test harness, package-data rules, and
overlay semantics are in the skill's [first-plugin guide](../.agents/skills/xbot-plugin-development/references/first-plugin.md).

Configuration is one plugin tree, resolved before XCore starts:

```text
XBotv2/xcore.yaml
  → <data-dir>/config/plugins.yaml
  → <workspace>/.xbot/plugins.yaml
  → <data-dir>/sessions/<session_id>/config.yaml
  → in-memory launch overrides
```

The session document uses the same `plugins` overlay schema. Sandbox and
permissions are the `sandbox` and `permissions` plugin declarations, not
separate configuration files.

Provider `request_timeout_seconds` defaults to 60 seconds and configures SDK
transport timeouts (connection, read inactivity, write, and pool acquisition).
It is not a total generation deadline: reasoning, text, or SSE heartbeats that
keep arriving may keep a request alive for longer. A provider that sends nothing
while thinking can still hit the read timeout; increase the value for that
endpoint, or set it to `null` to disable transport timeouts.

Timeout failures use the normal provider retry policy, including backoff and
`XBOT_PROVIDER_MAX_RETRIES`. The default remains 16 retries; `none`/`infinite`
explicitly requests unlimited retries. Once any model output has been emitted,
the request is not replayed, avoiding duplicated output or tool calls. Retry
exhaustion (or a failure after partial output) reaches the client as a normal
provider failure, not a separate deadline wrapper.
