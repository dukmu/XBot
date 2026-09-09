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
XCore plugin lifecycle, schema DSL, test harness, package-data rules, and
overlay semantics are in the skill's [first-plugin guide](../.agents/skills/xbot-plugin-development/references/first-plugin.md).
