"""Caption plugin: auto-title on the first user message + agent tool."""

import pytest
from plugin_harness import mount_plugin_standalone

from XBotv2.agentloop import EventContext, Events
from XBotv2.core import ConversationHistory, Message, ToolResult
from XBotv2.llm.mock import MockLLM
from XBotv2.session import SessionInfo

_SYSTEM = "You derive one short, human-readable title for a chat session."


def make_plugin(config=None):
    from XBotv2.caption.plugin import CaptionPlugin

    component = mount_plugin_standalone(CaptionPlugin(), config)
    return component.ctx.caption


class SetupContext:
    """Post-apply view of the plugin's registrations on a real XCore context."""

    def __init__(self, plugin) -> None:
        self.ctx = plugin._events
        entries = self.ctx.tools.registrations()
        self.tool = entries[0].tool if entries else None


def _event_context(messages, *, turn_count=1, parent_thread_id=""):
    session = SessionInfo("s", "agent", workspace_root="/work", turn_count=turn_count)
    ctx = EventContext(
        session=session,
        messages=list(messages),
    )
    if parent_thread_id:
        ctx.messages = list(messages)
    return ctx


@pytest.mark.asyncio
async def test_auto_caption_requests_an_independent_title_once():
    plugin = make_plugin({"auto": True, "allow_access": True})
    plugin.model = MockLLM(responses=[{"content": '  "Billing migration plan"  '}])
    original = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content="we need to port billing to the new ledger"),
    ]

    ctx = EventContext(
        session=SessionInfo("s", "agent", workspace_root="/work", turn_count=1),
        messages=list(original),
    )
    await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)

    assert plugin.title == "Billing migration plan"
    # The caption request is independent: the conversation is untouched.
    assert [message.content for message in ctx.messages] == [
        message.content for message in original
    ]
    # One caption per runtime, even if the hook fires again.
    await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)
    await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)
    assert plugin.model.call_count == 1


@pytest.mark.asyncio
async def test_auto_caption_skips_subagent_threads():
    plugin = make_plugin({"auto": True, "allow_access": True})
    plugin.model = MockLLM(responses=[{"content": "child title"}])
    original = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content="hello"),
    ]

    # A subagent thread never captions: the title is main-thread owned.
    sub = EventContext(
        session=SessionInfo("s", "child", workspace_root="/work", turn_count=1),
        messages=list(original),
    )
    plugin.state.replace(plugin.state.value.model_copy(
        update={"parent_thread_id": "agent"}
    ))
    await plugin._events.serial(Events.BEFORE_CONTEXT, sub)
    assert plugin.title == ""
    assert plugin.model.call_count == 0


@pytest.mark.asyncio
async def test_auto_caption_respects_an_existing_title():
    plugin = make_plugin({"auto": True, "allow_access": True})
    plugin.model = MockLLM(responses=[{"content": "overwrite me"}])
    plugin.state.update(title="User-set title")
    ctx = EventContext(
        session=SessionInfo("s", "agent", workspace_root="/work", turn_count=1),
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content="hi"),
        ],
    )
    await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)
    assert plugin.title == "User-set title"
    assert plugin.model.call_count == 0


@pytest.mark.asyncio
async def test_auto_caption_disabled_never_calls_the_model():
    plugin = make_plugin({"auto": False, "allow_access": True})
    plugin.model = MockLLM(responses=[{"content": "ignored"}])
    ctx = EventContext(
        session=SessionInfo("s", "agent", workspace_root="/work", turn_count=1),
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content="hi"),
        ],
    )
    await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)
    assert plugin.title == ""
    assert plugin.model.call_count == 0


def test_tool_registered_only_when_access_is_allowed():
    allowed = make_plugin({"auto": False, "allow_access": True})
    assert SetupContext(allowed).tool is not None

    denied = make_plugin({"auto": False, "allow_access": False})
    assert SetupContext(denied).tool is None


@pytest.mark.asyncio
async def test_caption_tool_gets_and_sets():
    plugin = make_plugin({"auto": False, "allow_access": True})
    plugin.model = MockLLM(responses=[])
    setup = SetupContext(plugin)
    assert setup.tool is not None

    result = await setup.tool.ainvoke({"action": "set", "title": "Ledger migration"})
    assert result.status == "success"
    assert plugin.title == "Ledger migration"

    result = await setup.tool.ainvoke({"action": "get"})
    assert result.status == "success"
    assert "Ledger migration" in str(result.content)

    bad = await setup.tool.ainvoke({"action": "set", "title": "   "})
    assert bad.status == "error"


@pytest.mark.asyncio
async def test_caption_tool_refuses_on_subagent_threads():
    plugin = make_plugin({"auto": False, "allow_access": True})
    plugin.model = MockLLM(responses=[])
    plugin.state.replace(plugin.state.value.model_copy(
        update={"parent_thread_id": "agent"}
    ))
    setup = SetupContext(plugin)
    result = await setup.tool.ainvoke({"action": "set", "title": "nope"})
    assert result.status == "error"
    assert "main thread" in str(result.content)


@pytest.mark.asyncio
async def test_caption_failure_is_silent():
    class BrokenModel:
        def __init__(self) -> None:
            self.call_count = 0

        async def astream(self, _messages, **_kwargs):
            self.call_count += 1
            if False:  # pragma: no cover - keeps this an async generator
                yield None
            raise RuntimeError("provider down")

    plugin = make_plugin({"auto": True, "allow_access": True})
    plugin.model = BrokenModel()
    ctx = EventContext(
        session=SessionInfo("s", "agent", workspace_root="/work", turn_count=1),
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content="hi"),
        ],
    )
    await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)  # must not raise
    assert plugin.title == ""
    assert plugin.model.call_count == 1
