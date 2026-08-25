# First Plugin: From Empty Directory to a Running Tool

This guide builds an external package named `my-xbot-plugin`. It works whether
XBot came from PyPI, a uv dependency, or a source checkout. Replace the names,
but preserve the ownership and test shape until the first plugin works.

## 1. Select the Runtime

The Python that launches XBot must also import the plugin. First record the
environment:

```bash
command -v xbot
python -c 'import sys, XBotv2, xcore; print(sys.executable); print(XBotv2.__file__); print(xcore.__file__)'
```

For a uv project, use `uv run xbot` and `uv run python` in every command. For a
pip installation, activate its virtual environment and use `python -m pip`,
`python -m pytest`, and that environment's `xbot`. Never install the plugin in
one interpreter and launch XBot from another.

## 2. Create the Package

Use this layout:

```text
my-xbot-plugin/
├── pyproject.toml
├── src/
│   └── my_xbot_plugin/
│       ├── __init__.py
│       └── plugin.py
└── tests/
    └── test_plugin.py
```

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "my-xbot-plugin"
version = "0.1.0"
description = "Example XBot greeting plugin"
requires-python = ">=3.11"
dependencies = ["xbotv2>=0.2"]

[project.optional-dependencies]
test = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Install for development using one mode:

```bash
# uv project
uv sync --extra test

# pip virtual environment
python -m pip install -e '.[test]'

# Developing against an XBot checkout instead of a published release:
uv add --editable /path/to/XBot
# or: python -m pip install -e /path/to/XBot
```

Do not put an absolute checkout path in committed dependency metadata. Use it
only as a local editable development source.

`src/my_xbot_plugin/__init__.py` may be empty. XBot tries
`my_xbot_plugin.plugin` before `my_xbot_plugin`, so put the conventional export
in `plugin.py`.

## 3. Implement the Plugin

## `plugin.py`

```python
from typing import Any

from XBotv2.core import Tool, ToolResult
from xcore import S


class HelloHandler:
    def __init__(self, greeting: str) -> None:
        self._greeting = greeting

    async def hello(self, name: str) -> ToolResult:
        """Greet one person and return the greeting to the Agent."""
        return ToolResult.success(f"{self._greeting}, {name}!")


class HelloPlugin:
    name = "hello"
    inject = ["tools"]
    Config = S.object({
        "greeting": S.string().default("Hello"),
    }).strict()

    def apply(self, ctx: Any, config: dict[str, object]) -> None:
        handler = HelloHandler(str(config["greeting"]))
        ctx.tools.register(Tool.from_function(handler.hello, name="hello"))


plugin = HelloPlugin()
```

Here `.strict()` means unknown keys are removed; it does not make unknown keys
an error. Required-key and type failures still raise `SchemaValidationError`
with a path. If rejecting every unknown config key is a requirement, the
current XCore schema contract does not provide that mode—document the limit
instead of claiming strict rejection.

The component only composes declared dependencies. XCore validates `Config`
before `apply`, waits for `tools`, and reruns composition if that required
service is later removed and restored. The named handler owns Tool behavior and
receives only the exact value it needs; it does not retain `Context`.
`ctx.tools.register` binds cleanup to the current XCore fiber, so unload removes
the Tool automatically.

Avoid these common first attempts:

```python
# Do not keep the service locator in business code.
self.ctx = ctx

# Do not hide a required dependency behind probing or a fallback.
tools = getattr(ctx, "tools", None)
if tools is not None:
    ...

# Do not register a closure that captures the entire component.
async def hello(name: str):
    return await self._do_work(name)
```

Required dependencies belong in `inject`; optional dependencies are only for a
real documented feature mode in which absence is valid.

## 4. Test Composition and Behavior

Use a real XCore lifecycle plus the XBot Tool service. This test imports two
implementation classes only to construct the smallest realistic registry;
plugin production code still imports public contracts from package roots.

```python
from pathlib import Path

from xcore import Context, FiberState

from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_service import ToolsService
from my_xbot_plugin.plugin import plugin


async def test_hello_registers_invokes_and_unloads(tmp_path: Path) -> None:
    ctx = Context(data_dir=tmp_path)
    tools = ToolsService(ToolRegistry())
    ctx.set("tools", tools)
    handle = ctx.plugin(plugin, {"greeting": "Welcome"})

    await ctx.start()

    assert handle.state is FiberState.RUNNING
    tool = tools.resolve("hello")
    assert tool is not None
    result = await tool.ainvoke({"name": "Ada"})
    assert result.content == "Welcome, Ada!"
    assert tool.provider_schema()["function"]["parameters"]["required"] == ["name"]

    await ctx.stop()
    assert tools.resolve("hello", include_disabled=True) is None


async def test_hello_waits_for_required_tools(tmp_path: Path) -> None:
    ctx = Context(data_dir=tmp_path)
    handle = ctx.plugin(plugin, {})

    await ctx.start()

    assert handle.state is FiberState.PENDING
    assert handle.missing_dependencies == ("tools",)
    await ctx.destroy()
```

Run it with the selected environment:

```bash
uv run pytest -q
# or, in the pip virtual environment:
python -m pytest -q
```

If the test collects zero items, imports XBot from an unexpected path, or uses
a global `pytest` executable, stop and fix the environment before trusting it.

## 5. Add the Tree Entry

## Tree Entry

Create `.xbot/plugins.yaml` in the workspace used for the smoke test:

```yaml
- id: hello
  name: my_xbot_plugin
  profiles: [agent]
  config:
    greeting: "Hello"
```

The `name` is the import package, not the distribution name (`my_xbot_plugin`,
not `my-xbot-plugin`). A new overlay entry needs both `id` and `name`. Use an
`agent` profile for an Agent Tool. Do not add a server-only plugin to the Agent
profile merely because both processes can import it.

XBot merges configuration in this order: bundled `xcore.yaml`, configured
`<data-dir>/config/plugins.yaml`, workspace `.xbot/plugins.yaml`, then explicit
session/direct-call overrides. Only `config` deep-merges; fields such as
`profiles` replace the prior value.

## 6. Smoke Test Through XBot

Use disposable paths first:

```bash
mkdir -p /tmp/xbot-plugin-smoke/data /tmp/xbot-plugin-smoke/workspace/.xbot
# copy the overlay above to /tmp/xbot-plugin-smoke/workspace/.xbot/plugins.yaml
xbot once --data-dir /tmp/xbot-plugin-smoke/data \
  --workspace /tmp/xbot-plugin-smoke/workspace \
  "Call the hello tool with the name Ada and report its exact result."
```

Prefix with `uv run` for uv. A real provider requires its configured
credentials. If none is available, the mounted test is still valid evidence
for loading, registration, execution, and cleanup; report that the provider
smoke was not run rather than claiming it passed.

When startup says the module cannot be imported, rerun the provenance command
from step 1 and then:

```bash
python -c 'import my_xbot_plugin, my_xbot_plugin.plugin; print(my_xbot_plugin.__file__)'
```

When startup reports unmet dependencies, check the selected profile and the
plugin's `inject` names against the mounted tree. Do not add a `None` fallback
to silence the error.

## 7. Add Tool Invocation Metadata Only When Needed

If the Tool must know which final model call invoked it, use the standard
`ToolCall` parameter rather than a session or invocation wrapper:

```python
from XBotv2.core import ToolCall, ToolResult


async def inspect_call(value: str, *, tool_call: ToolCall) -> ToolResult:
    return ToolResult.success(f"{tool_call.id}: {value}")
```

`tool_call` is keyword-only, excluded from the provider schema, and receives
the call after `BEFORE_TOOL_CALL` rewriting. Sandbox, jobs, approval, and other
services should instead be constructor dependencies of a named handler.

## 8. Add Persistent State Without Choosing a File

State is neither plugin configuration nor a plugin-chosen file. Resolve one
namespace during composition and pass it to the owning service:

```python
class CounterService:
    def __init__(self, state) -> None:
        self._state = state

    async def increment(self) -> ToolResult:
        stored = await self._state.get("count")
        value = (0 if stored is None else int(stored)) + 1
        await self._state.set("count", value)
        return ToolResult.success(str(value), data={"count": value})


class CounterPlugin:
    name = "counter"
    inject = ["tools"]

    def apply(self, ctx, config=None) -> None:
        service = CounterService(ctx.state.namespace("counter"))
        ctx.tools.register(Tool.from_function(service.increment))
```

Do not join `data_dir`, `plugin_state`, or a JSON/YAML filename yourself.

State values must be JSON-compatible. Store stable domain data, not an HTTP
client, coroutine, event waiter, `Path`, dataclass instance, or duplicated
conversation history. To prove recovery, stop the first Context, construct a
new Context with the same `data_dir`, and read through the same namespace.

## 9. Build and Inspect the Distribution

Build a wheel in a clean output directory:

```bash
uv build --wheel
# or: python -m build --wheel
```

Install that wheel into a fresh virtual environment and rerun provenance,
imports, and plugin tests. Also inspect the archive when the plugin bundles
templates, prompts, skills, or reference files; setuptools includes Python
packages by default but non-Python assets require explicit package-data
configuration.

## 10. Completion Checklist

- the same interpreter owns XBot, xcore, the plugin, and pytest;
- the plugin has a stable import package and conventional `plugin` export;
- `Config` rejects malformed values and documents defaults;
- every required service is in `inject` and passed narrowly to a named owner;
- public behavior is invoked, not inferred from private fields;
- unload removes registrations and closes resources;
- durable state uses one namespace and survives a new Context;
- the intended XBot profile loads the overlay from the intended workspace/data
  directory;
- the built wheel installs and contains every required non-Python asset;
- skipped provider, browser, network, or sandbox smoke tests are recorded.

Continue with [extension-patterns.md](extension-patterns.md) for commands,
events, shared services, transactional dynamic registration, and external
resource cleanup.
