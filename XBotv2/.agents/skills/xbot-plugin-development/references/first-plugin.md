# First Plugin: Hello Tool

This example is intentionally small. Replace `my_xbot_plugin` with a package
inside the XBot checkout or with an importable external package. For an
external package, install it into the same environment as XBot (`uv add
my-xbot-plugin`, `uv pip install -e .`, or `python -m pip install -e .`). The
loader looks for `<name>.plugin` first, so the example uses
`my_xbot_plugin/plugin.py`.

## `plugin.py`

```python
from typing import Any

from XBotv2.core import Tool, ToolResult


class HelloPlugin:
    name = "hello"
    inject = ["tools"]

    def __init__(self) -> None:
        self._runtime_tools: list[str] = []

    def apply(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        config = config or {}
        greeting = str(config.get("greeting") or "Hello")
        ctx.dispose(self._cleanup)

        async def hello(name: str) -> ToolResult:
            """Greet one person and return the greeting to the Agent."""
            return ToolResult.success(f"{greeting}, {name}!")

        ctx.tools.register(Tool.from_function(hello, name="hello"))

    def _cleanup(self) -> None:
        # Static registrations made during apply are fiber effects. Only keep
        # explicit names here for registrations made later from an event.
        for registered_name in reversed(self._runtime_tools):
            self.ctx.tools.unregister(registered_name)
        self._runtime_tools.clear()


plugin = HelloPlugin()
```

The example's point is the ownership shape: the
Tool is built in the plugin, its service dependency is declared, and cleanup
belongs to the plugin.

## Tree Entry

Add this to the relevant plugin-tree overlay, commonly `.xbot/plugins.yaml`:

```yaml
- id: hello
  name: my_xbot_plugin
  profiles: [agent]
  config:
    greeting: "Hello"
```

Use an `agent` profile for an Agent Tool. Do not add a server-only plugin to the
Agent profile merely because both processes can import it.

## A Tool Metadata Example

If the Tool must know which final model call invoked it, use the standard
`ToolCall` parameter rather than a session or invocation wrapper:

```python
from XBotv2.core import ToolCall, ToolResult


async def inspect_call(value: str, *, tool_call: ToolCall) -> ToolResult:
    return ToolResult.success(f"{tool_call.id}: {value}")
```

`tool_call` is keyword-only, excluded from the provider schema, and receives
the call after `BEFORE_TOOL_CALL` rewriting. Sandbox, jobs, approval, and other
services should instead be captured by a factory or closure.

## Test Shape

Create a test in the plugin package's test tree. The exact fixtures vary by
installation mode, but the observable assertions should look like this:

```python
import asyncio

from XBotv2.core import Tool


def test_hello_tool() -> None:
    async def hello(name: str) -> str:
        return f"Hello, {name}!"

    tool = Tool.from_function(hello, name="hello")
    assert asyncio.run(tool.ainvoke({"name": "Ada"})) == "Hello, Ada!"
    assert tool.provider_schema()["function"]["name"] == "hello"
```

For a mounted plugin, add tests for registration, unload, duplicate names,
configuration, missing optional services, and a failed dynamic registration.
Run them with the interpreter selected by the parent skill. In a `uv` project
use `uv run pytest`; in a pip environment use `python -m pytest`. If the
plugin is loaded by a source checkout, also run that checkout's architecture
and compile checks; these are not available from an installed wheel.
