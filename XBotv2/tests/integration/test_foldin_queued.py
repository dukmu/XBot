"""Fold-in queued-message regression tests.

A queued user message that is accepted mid-turn (after a complete ToolResult
batch) must:

- emit a ``turn_started`` boundary on the queued request's stream so the TUI
  can append the user's text;
- deliver the post-fold events exactly once (no duplication across the active
  and the folded request streams);
- deliver each ``usage`` event exactly once so token counters stay accurate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import Tool
from XBotv2.llm.mock import MockLLM
from XBotv2.application.server import start_server_application


@pytest_asyncio.fixture
async def foldin_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "provider": "openai",
                            "model": "test",
                            "base_url": "http://test",
                            "api_key": "test",
                            "max_context_tokens": 4096,
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "test",
                        "user_name": "Tester",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )
    (data_dir / "config" / "config.yaml").write_text(
        "provider: default\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    try:
        yield server.server
    finally:
        await server.stop()


async def _collect(stream):
    return [event async for event in stream]


async def _run_foldin(app, llm):
    ctx = await app.state.manager.open_session(
        session_id="foldin",
        thread_id="t",
        provider_name="default",
        workspace_root=str(app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.services.permissions.replace_rules({"allow": [{"tool": ".*"}]})
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.engine.tools.registry.register(Tool.from_function(wait_for_release))
    ctx.engine.tools.registry.restrict(None)

    ev_stream = ctx.attach_event_stream()

    async def _collect_events(stream):
        events = [event async for event in stream]
        return events

    first_task = asyncio.create_task(
        _collect_events(ctx.stream_message("first request", "req-1"))
    )
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    second_task = asyncio.create_task(
        _collect_events(ctx.stream_message("second queued", "req-2"))
    )
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 1
    release_tool.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)
    message_events = []
    try:
        async with asyncio.timeout(1):
            while True:
                event = await ev_stream.get()
                if event is None:
                    break
                if event.get("type") == "message":
                    message_events.append(event)
    except TimeoutError:
        pass
    return first_events, second_events, message_events


@pytest.mark.asyncio
async def test_foldin_emits_turn_started_and_no_duplicate(foldin_app) -> None:
    llm = MockLLM(responses=[
        {
            "tool_calls": [{
                "id": "wait-1",
                "name": "wait_for_release",
                "args": {"value": "ready"},
            }],
            "usage_metadata": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "requests": 1,
            },
        },
        {
            "content": "handled both",
            "usage_metadata": {
                "input_tokens": 200,
                "output_tokens": 80,
                "total_tokens": 280,
                "requests": 1,
            },
        },
    ])
    first_events, second_events, message_events = await _run_foldin(foldin_app, llm)

    # The folded-in input is notified on the shared event stream with the
    # server-side id and content, so the client renders it from the event.
    assert any(
        event["data"].get("role") == "user"
        and event["data"].get("content") == "second queued"
        and event["data"].get("id")
        for event in message_events
    ), "folded-in input must receive a message event with an id"

    # The response must be delivered exactly once across both streams.
    combined = [
        event
        for events in (first_events, second_events)
        for event in events
        if event["type"] == "assistant_message"
    ]
    contents = [event["data"].get("content") for event in combined]
    assert contents.count("handled both") == 1, (
        f"duplicate delivery of fold-in response: {contents}"
    )

    # Each usage event must be applied exactly once.
    usage_totals = [
        event["data"].get("total_tokens")
        for events in (first_events, second_events)
        for event in events
        if event["type"] == "usage"
    ]
    assert usage_totals.count(280) == 1, (
        f"usage event delivered more than once: {usage_totals}"
    )

    # The fold-in response belongs only to the queued request; the active
    # request must not observe it (single event source after hand-off).
    assert not any(
        event["type"] == "assistant_message"
        and event["data"].get("content") == "handled both"
        for event in first_events
    )
    assert llm.call_count == 2


async def _run_multi_queue(app, llm):
    """First turn blocks on a tool; SECOND and THIRD are queued meanwhile."""
    ctx = await app.state.manager.open_session(
        session_id="multi",
        thread_id="t",
        provider_name="default",
        workspace_root=str(app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.services.permissions.replace_rules({"allow": [{"tool": ".*"}]})
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.engine.tools.registry.register(Tool.from_function(wait_for_release))
    ctx.engine.tools.registry.restrict(None)

    async def collect(stream):
        events = []
        async for event in stream:
            events.append(event)
        return events

    ev_stream = ctx.attach_event_stream()
    first_task = asyncio.create_task(collect(ctx.stream_message("first", "req-1")))
    await asyncio.wait_for(tool_started.wait(), timeout=3)
    second_task = asyncio.create_task(collect(ctx.stream_message("second", "req-2")))
    third_task = asyncio.create_task(collect(ctx.stream_message("third", "req-3")))
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 2
    release_tool.set()
    first_events = await asyncio.wait_for(first_task, timeout=5)
    second_events = await asyncio.wait_for(second_task, timeout=5)
    third_events = await asyncio.wait_for(third_task, timeout=5)
    message_events = []
    try:
        async with asyncio.timeout(1):
            while True:
                event = await ev_stream.get()
                if event is None:
                    break
                if event.get("type") == "message":
                    message_events.append(event.get("data", {}).get("content"))
    except TimeoutError:
        pass
    return first_events, second_events, third_events, message_events


@pytest.mark.asyncio
async def test_multiple_queued_messages_all_drain_in_order(foldin_app) -> None:
    """Every queued message must be injected at once, fused into one turn.

    While the agent is busy the mailbox holds the queue; at the tool
    boundary ALL pending messages are fused into the turn context (one LLM
    call), each queued stream receives a ``turn_started`` pop signal, and the
    final queued stream owns the single merged reply.

    Regression: only one queued message was folded per boundary, so the
    second queued message never drained and the transcript did not update."""

    llm = MockLLM(responses=[
        {"tool_calls": [{"id": "w1", "name": "wait_for_release", "args": {"value": "x"}}]},
        {"content": "handled first second and third"},
    ])
    first_events, second_events, third_events, message_events = await _run_multi_queue(foldin_app, llm)

    # The active stream keeps its own tool result (no cross-stream leakage).
    first_tool_results = [
        event["data"].get("content")
        for event in first_events
        if event["type"] == "tool_result"
    ]
    assert first_tool_results == ["x"], first_tool_results
    assert not any(
        event["type"] == "tool_result"
        for event in second_events + third_events
    ), "fused streams must not receive the active turn's tool_result"

    # All inputs are notified in submission order on the shared stream.
    assert message_events == ["first", "second", "third"], message_events

    # The fused reply is delivered exactly once, to the final queued stream.
    third_replies = [
        event["data"].get("content")
        for event in third_events
        if event["type"] == "assistant_message"
    ]
    assert "handled first second and third" in third_replies, third_replies
    assert not any(
        event["type"] == "assistant_message" and event["data"].get("content")
        for event in second_events
    ), "non-final queued stream must not receive the merged reply"

    # Both queued messages were consumed by ONE model call after the fusion.
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_background_task_completion_reaches_tui_task_panel(foldin_app) -> None:
    """A completed background job must publish a terminal ``task_updated`` so
    the TUI task panel stops showing it as running.

    Regression: ``JobRegistry._finish`` only fired ``on_complete`` (a
    completion notice the TUI never applies); no terminal ``task_updated``
    reached live clients, so tasks stayed "running" forever."""

    from XBotv2.core.jobs import JobKind
    from XBotv2.coretools.shell import SHELL_TOOLS

    ctx = await foldin_app.state.manager.open_session(
        session_id="task-panel",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "hi"}]),
    )
    ctx.services.permissions.replace_rules({"allow": [{"tool": ".*"}]})
    events = ctx.attach_event_stream()

    registry = ctx.services.jobs
    assert registry is not None
    tools = {tool.name: tool for tool in SHELL_TOOLS}
    started = await tools["shell"].ainvoke(
        {"command": "echo done", "background": True},
        job_registry=registry,
        sandbox=None,
        sandbox_policy=None,
    )
    job_id = started.data["id"]
    await registry.wait([job_id])

    task_updates = []
    async with asyncio.timeout(1):
        while True:
            event = await events.get()
            if event is None:
                break
            if event.get("type") == "task_updated":
                task_updates.append(event["data"].get("status"))
            if "completed" in task_updates:
                break
    assert "completed" in task_updates, task_updates
    assert "running" in task_updates, task_updates
    assert task_updates.index("running") < task_updates.index("completed"), task_updates
