from types import SimpleNamespace

from XBotv2.agentloop.api.contracts import AllTools
from XBotv2.agentloop.impl.engine import Engine
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.core.api.domain import (
    GenerationSettings, ModelRoute, ResolvedModelSelection, StandardGenerationMode,
)
from XBotv2.core.api.tools import Tool


async def test_requests_reuse_schemas_and_follow_tool_changes():
    def first(value: int) -> str:
        return str(value)

    def second(value: str) -> str:
        return value

    tools = ToolsService(ToolRegistry(), ownership="caller")
    original = Tool.from_function(first)
    tools.register(original)
    selection = ResolvedModelSelection(
        route=ModelRoute(provider="mock", model="test"),
        generation=GenerationSettings(mode=StandardGenerationMode(), max_output_tokens=128),
        context_window=4096,
    )
    state = SimpleNamespace(
        session=SimpleNamespace(),
        metadata=SimpleNamespace(value=SimpleNamespace(runtime_selection=SimpleNamespace(model=selection))),
        history=SimpleNamespace(surface_revision=0),
    )
    engine = Engine(model_client=object(), tools=tools, events=object(), state=state, user_identity=object(), memory="", inbox=object())

    async def dispatch(*args, **kwargs):
        return None

    async def observe(request):
        return request

    engine._dispatch = dispatch
    engine._observe_model_request = observe

    async def request():
        return await engine._prepare_model_request([], finalizing=False)

    one = await request()
    two = await request()
    assert one.tools[0] is two.tools[0]
    assert one.tools[0].parameters == original.parameters
    tools.register(Tool.from_function(second))
    both = await request()
    assert [item.name for item in both.tools] == ["first", "second"]
    assert both.tools[0] is one.tools[0]
    tools.restrict(("second",))
    assert [item.name for item in (await request()).tools] == ["second"]
    tools.restrict(AllTools())
    tools.exclude(["first"])
    assert [item.name for item in (await request()).tools] == ["second"]
    tools.restrict(AllTools())
    tools.unregister("second")
    assert [item.name for item in (await request()).tools] == ["first"]
