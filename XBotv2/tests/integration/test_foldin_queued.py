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

from xbotv2.api.paths import RuntimePaths
from xbotv2.api.tools import Tool
from xbotv2.llm.mock import MockLLM
from xbotv2.protocol.http_server import create_app
from xbotv2.tools.permissions import PermissionSystem


@pytest_asyncio.fixture
async def foldin_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "providers.yaml").write_text(
        "default: default\nproviders:\n  default:\n    provider: openai\n"
        "    model: test\n    base_url: http://test\n    api_key: test\n"
        "    max_context_tokens: 4096\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "user.yaml").write_text(
        "user_id: test\nuser_name: Tester\nplatform: tui\n"
        "session_type: interactive\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "config.yaml").write_text(
        "provider: default\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    yield app


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
    ctx.engine.permission_system = PermissionSystem(default_decision="allow")
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.engine.tool_registry.register(Tool.from_function(wait_for_release))
    ctx.engine.tool_registry.restrict(None)

    first_task = asyncio.create_task(
        _collect(ctx.stream_message("first request", "req-1"))
    )
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    second_stream = ctx.stream_message("second queued", "req-2")
    queued = await anext(second_stream)
    assert queued["type"] == "message_queued"
    release_tool.set()
    first_events, second_events = await asyncio.gather(
        first_task, _collect(second_stream)
    )
    return first_events, second_events


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
    first_events, second_events = await _run_foldin(foldin_app, llm)

    # The folded-in request must receive a turn boundary so the TUI can show
    # the queued user text.
    assert any(
        event["type"] == "turn_started" for event in second_events
    ), "folded-in request must receive turn_started"

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
