"""Caption plugin: auto-title on the first user message + agent tool."""

import asyncio

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

    # A subagent thread never captions: the main thread owns the title.
    plugin._is_subagent = True
    sub = EventContext(
        session=SessionInfo("s", "child", workspace_root="/work", turn_count=1),
        messages=list(original),
    )
    await plugin._events.serial(Events.BEFORE_CONTEXT, sub)
    assert plugin.title == ""
    assert plugin.model.call_count == 0


@pytest.mark.asyncio
async def test_auto_caption_respects_an_existing_title():
    plugin = make_plugin({"auto": True, "allow_access": True})
    plugin.model = MockLLM(responses=[{"content": "overwrite me"}])
    await plugin.state.update(title="User-set title")
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
    await plugin.state.replace(plugin.state.value.model_copy(
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


@pytest.mark.asyncio
async def test_caption_title_reaches_the_session_catalog(tmp_path):
    """A caption wrote the title; open clients must hear about it.

    Nothing in the caption path knows about events: it writes the metadata
    value, the runtime announces the change, and the process-level session
    owner turns that into the catalog change clients already consume.
    """
    from XBotv2.application.app import create_agent_application
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM
    from XBotv2.session.contracts import (
        AgentApplicationOptions,
        OpenSession,
        SESSION_RESOURCE_CHANGED,
    )
    from XBotv2.session.manager import SessionManager

    class _Events:
        def __init__(self) -> None:
            self.seen: list[tuple[str, object]] = []

        async def emit(self, event, *args) -> None:
            self.seen.append((event, args))

    paths = RuntimePaths.from_data_dir(tmp_path)
    events = _Events()
    model = MockLLM(responses=[
        {"content": "Billing migration plan"},
        {"content": "acknowledged"},
    ])

    async def factory(options):
        return await create_agent_application(AgentApplicationOptions(
            paths=options.paths,
            provider_name=options.provider_name,
            session_id=options.session_id,
            thread_id=options.thread_id,
            workspace_root=options.workspace_root,
            no_plugins=False,
            model_override=model,
        ))

    manager = SessionManager(
        paths,
        events,
        thread_persistence_factory=None,
        application_factory=factory,
        idle_timeout=None,
    )
    try:
        await manager.open(OpenSession(
            session_id="caption-catalog",
            thread_id="agent",
            provider_name="default",
            workspace_root=str(tmp_path),
            no_plugins=False,
            mode="new",
        ))
        runtime = await manager.get("caption-catalog", "agent")
        events.seen.clear()

        async for _event in runtime.engine.run_turn("port billing to the ledger"):
            pass
        for _ in range(20):
            if any(name == SESSION_RESOURCE_CHANGED for name, _ in events.seen):
                break
            await asyncio.sleep(0.05)

        summaries = [
            args[0].session
            for name, args in events.seen
            if name == SESSION_RESOURCE_CHANGED
        ]
        assert summaries, [name for name, _ in events.seen]
        assert summaries[-1].title == "Billing migration plan"
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_metadata_change_stays_off_the_runtime_event_stream(tmp_path):
    """Metadata mutations announce on the app bus — never on the runtime stream.

    The bounded replay stream is the transport boundary for runtime/protocol
    events only; a title write (or any metadata mutation) surfaces to clients
    through catalog events, so subscribers of one channel never see the other
    channel's traffic.
    """
    from XBotv2.application.app import create_agent_application
    from XBotv2.core.metadata import THREAD_METADATA_CHANGED
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM
    from XBotv2.session.contracts import (
        AgentApplicationOptions,
        OpenSession,
        SESSION_RESOURCE_CHANGED,
    )
    from XBotv2.session.manager import SessionManager

    class _Events:
        def __init__(self) -> None:
            self.seen: list[tuple[str, object]] = []

        async def emit(self, event, *args) -> None:
            self.seen.append((event, args))

    paths = RuntimePaths.from_data_dir(tmp_path)
    events = _Events()
    model = MockLLM(responses=[{"content": "acknowledged"}])

    async def factory(options):
        return await create_agent_application(AgentApplicationOptions(
            paths=options.paths,
            provider_name=options.provider_name,
            session_id=options.session_id,
            thread_id=options.thread_id,
            workspace_root=options.workspace_root,
            no_plugins=False,
            model_override=model,
        ))

    manager = SessionManager(
        paths,
        events,
        thread_persistence_factory=None,
        application_factory=factory,
        idle_timeout=None,
    )
    try:
        opened = await manager.open(OpenSession(
            session_id="boundary-check",
            thread_id="agent",
            provider_name="default",
            workspace_root=str(tmp_path),
            no_plugins=False,
            mode="new",
        ))
        runtime = await manager.get("boundary-check", "agent")
        events.seen.clear()

        bus_changes = []
        runtime.application.events.on(
            THREAD_METADATA_CHANGED,
            lambda change: bus_changes.append(change),
        )
        stream = runtime.attach_event_stream(after=opened.event_cursor)

        await runtime.application.loop_state.metadata.update(title="Stream check")

        # The write announced on the application bus with both values.
        assert [
            (change.previous.title, change.current.title)
            for change in bus_changes
        ] == [("", "Stream check")]
        # The process-level catalog owner turned that into the catalog event
        # clients consume.
        assert [name for name, _ in events.seen] == [SESSION_RESOURCE_CHANGED]
        # The runtime replay stream is the transport boundary for runtime
        # events only: the title change published nothing on it.
        assert runtime.event_stream.sequence == opened.event_cursor
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_transient_provider_failure_retries_caption_on_next_turn():
    """A failed caption request must not permanently disable auto-title: the
    next turn retries (the failure leaves the request state clean)."""
    plugin = make_plugin({"auto": True, "allow_access": True})
    plugin.model = MockLLM(responses=[{"content": "retried title"}])

    async def fail_once(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    original = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content="first message"),
    ]

    # First turn: the provider fails; no title is applied and no permanent
    # flag is set.
    import XBotv2.caption.service as caption_service

    failing = caption_service.invoke_llm
    caption_service.invoke_llm = fail_once
    try:
        ctx = EventContext(
            session=SessionInfo("s", "agent", workspace_root="/work", turn_count=1),
            messages=list(original),
        )
        await plugin._events.serial(Events.BEFORE_CONTEXT, ctx)
    finally:
        caption_service.invoke_llm = failing

    assert plugin.title == ""
    assert plugin.model.call_count == 0

    # Second turn: the retry succeeds and captions the session.
    retry_ctx = EventContext(
        session=SessionInfo("s", "agent", workspace_root="/work", turn_count=1),
        messages=list(original),
    )
    await plugin._events.serial(Events.BEFORE_CONTEXT, retry_ctx)
    assert plugin.title == "retried title"
