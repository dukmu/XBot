"""End-to-end checks for the loop-owned input path."""

import asyncio

import pytest

from XBotv2.session.runtime import SessionRuntime
from XBotv2.application import start_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import Tool
from XBotv2.llm.mock import MockLLM


@pytest.mark.asyncio
async def test_busy_user_input_is_claimed_from_next_step_without_content_side_queue(
    temp_data_dir,
    temp_workspace,
):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocker() -> str:
        """Wait until the test submits a steering input."""
        started.set()
        await release.wait()
        return "done"

    provider = MockLLM(responses=[
        {"tool_calls": [{"id": "call-1", "name": "blocker", "args": {}}]},
        {"content": "merged reply"},
    ])
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await start_application(
        paths=paths,
        session_id="inbox-routing",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    engine = services.engine
    services.permissions.configure({"allow": [{"tool": ".*"}]})
    engine.tools.registry.register(Tool.from_function(blocker))
    runtime = SessionRuntime(
        "inbox-routing",
        "agent",
        "default",
        paths,
        str(temp_workspace),
        False,
        services,
        engine,
    )

    async def collect(content: str, request_id: str):
        return [
            event
            async for event in runtime.stream_message(content, request_id)
        ]

    first = asyncio.create_task(collect("first", "first-id"))
    await started.wait()
    second = asyncio.create_task(collect("steer", "steer-id"))
    await asyncio.sleep(0)
    release.set()
    first_events, second_events = await asyncio.gather(first, second)

    assert [message.content for message in engine.messages] == [
        "first", "", "done", "steer", "merged reply",
    ]
    assert not runtime.pending_responses
    assert engine.pending_input_count == 0
    assert first_events[-1]["type"] == "tool_result"
    assert second_events[-1]["type"] == "turn_finished"


@pytest.mark.asyncio
async def test_injected_notification_is_durable_and_does_not_wake(
    temp_data_dir,
    temp_workspace,
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await start_application(
        paths=paths,
        session_id="durable-inbox",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    engine = services.engine
    runtime = SessionRuntime(
        "durable-inbox",
        "agent",
        "default",
        paths,
        str(temp_workspace),
        False,
        services,
        engine,
    )
    await engine.inject("job finished", source="job", message_id="job-1")

    assert engine.pending_input_count == 1
    assert runtime.wakeup_task is None

    resumed_services = await start_application(
        paths=paths,
        session_id="durable-inbox",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(responses=[{"content": "observed"}]),
    )
    resumed = resumed_services.engine
    assert resumed.pending_input_count == 1
    events = [
        event
        async for event in resumed.run_turn("continue", request_id="user-1")
    ]
    assert resumed.pending_input_count == 0
    assert [message.content for message in resumed.messages] == [
        "job finished", "continue", "observed",
    ]
    assert events[-1]["type"] == "turn_finished"
    await resumed_services.stop()
    await services.stop()
