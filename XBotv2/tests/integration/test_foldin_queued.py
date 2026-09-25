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
import json
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
import httpx

from XBotv2.core.paths import RuntimePaths
from XBotv2.agentloop import AllTools
from XBotv2.core.tools import Tool, ToolSucceeded
from XBotv2.permissions import PermissionPolicy, PermissionRule
from XBotv2.llm.mock import MockLLM
from XBotv2.application.server import start_server_application
from XBotv2.agentloop.protocol import (
    AssistantCompleted,
    LoopError,
    LoopTurnEnded,
    TurnCancelled,
    ToolCompleted,
)
from XBotv2.core.parts import TextPart
from XBotv2.jobs.protocol import JobCompletedEvent, JobUpdatedEvent
from XBotv2.session.protocol import MessagePublishedEvent
from XBotv2.session.runtime import start_regenerate_turn
from XBotv2.usage import UsageUpdated


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
                    "default_provider": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                    "max_output_tokens": 1024,
                                },
                            ],
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
    server.server.state.manager = server.sessions
    server.server.state.paths = server.runtime_paths
    try:
        yield server.server
    finally:
        await server.stop()


async def _collect(stream):
    return [event async for event in stream]


def _runtime_command(runtime, content: str, request_id: str, *, delivery="steer"):
    """Issue a command while consuming the central session event stream."""
    async def stream():
        events = runtime.event_stream.subscribe()
        try:
            await runtime.send_message(content, request_id, delivery=delivery)
            async for frame in events:
                yield frame.event
                if isinstance(frame.event, LoopTurnEnded):
                    break
                if (
                    isinstance(frame.event, LoopError)
                    and frame.event.code == "turn_failed"
                ):
                    break
        finally:
            await events.aclose()
    return stream()


@pytest.mark.asyncio
async def test_agent_application_snapshot_without_optional_plugins(foldin_app):
    runtime = await foldin_app.state.manager.open_session(
        session_id="snapshot",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "ok"}]),
    )

    snapshot = await asyncio.wait_for(runtime.application.snapshot(), timeout=2)
    assert snapshot.metadata.runtime_selection.agent_name


@pytest.mark.asyncio
async def test_http_open_snapshot_without_optional_plugins(foldin_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=foldin_app),
        base_url="http://test",
    ) as client:
        response = await asyncio.wait_for(
            client.post("/sessions", json={"session_id": "snapshot-http", "thread_id": "t"}),
            timeout=2,
        )
    assert response.status_code == 200


async def _run_foldin(app, llm):
    ctx = await app.state.manager.open_session(
        session_id="foldin",
        thread_id="t",
        provider_name="default",
        workspace_root=str(app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    services = ctx.application._context
    services.permissions.replace_policies((PermissionPolicy(
        rules=(PermissionRule(tool_pattern=".*", decision="allow"),),
        default_decision="ask",
    ),))
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    services.tools._registry.register(Tool.from_function(wait_for_release))
    services.tools._registry.restrict(AllTools())

    ev_stream = ctx.event_stream.subscribe()

    async def _collect_events(stream):
        return [event async for event in stream]

    first_task = asyncio.create_task(
        _collect_events(_runtime_command(ctx, "first request", "req-1"))
    )
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    second_task = asyncio.create_task(
        _collect_events(_runtime_command(ctx, "second queued", "req-2"))
    )
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 1
    release_tool.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)
    message_events = []
    try:
        async with asyncio.timeout(1):
            while True:
                event = (await anext(ev_stream)).event
                if isinstance(event, MessagePublishedEvent):
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
        event.record.root.kind == "human_input"
        and event.record.root.content == "second queued"
        and event.record.root.id
        for event in message_events
    ), "folded-in input must receive a message event with an id"

    # Both consumers are views of the one central stream, so each observes
    # the authoritative response exactly once.
    combined = [
        event
        for events in (first_events, second_events)
        for event in events
        if isinstance(event, AssistantCompleted)
    ]
    contents = [
        "".join(part.text for part in event.message.parts if isinstance(part, TextPart))
        for event in combined
    ]
    assert contents.count("handled both") == 2, (
        f"central response was not observed by both subscribers: {contents}"
    )

    # Each usage event must be applied exactly once.
    usage_totals = [
        event.snapshot.total_counters.output
        for events in (first_events, second_events)
        for event in events
        if isinstance(event, UsageUpdated)
    ]
    assert usage_totals.count(130) == 2, (
        f"central usage event was not observed by both subscribers: {usage_totals}"
    )

    # The fold-in response belongs only to the queued request; the active
    # request must not observe it (single event source after hand-off).
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
    services = ctx.application._context
    services.permissions.replace_policies((PermissionPolicy(
        rules=(PermissionRule(tool_pattern=".*", decision="allow"),),
        default_decision="ask",
    ),))
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    services.tools._registry.register(Tool.from_function(wait_for_release))
    services.tools._registry.restrict(AllTools())

    async def collect(stream):
        events = []
        async for event in stream:
            events.append(event)
        return events

    ev_stream = ctx.event_stream.subscribe()
    first_task = asyncio.create_task(collect(_runtime_command(ctx, "first", "req-1")))
    await asyncio.wait_for(tool_started.wait(), timeout=3)
    second_task = asyncio.create_task(collect(_runtime_command(ctx, "second", "req-2")))
    third_task = asyncio.create_task(collect(_runtime_command(ctx, "third", "req-3")))
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
                event = (await anext(ev_stream)).event
                if isinstance(event, MessagePublishedEvent):
                    message_events.append(event.record.root.content)
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

    # Every consumer follows the authoritative central stream.
    first_tool_results = [
        "".join(
            part.text
            for part in event.execution.message.outcome.output.parts
            if isinstance(part, TextPart)
        )
        for event in first_events
        if isinstance(event, ToolCompleted)
    ]
    assert first_tool_results == ["x"], (
        first_tool_results,
        [type(event).__name__ for event in first_events],
    )
    assert [
        "".join(
            part.text
            for part in event.execution.message.outcome.output.parts
            if isinstance(part, TextPart)
        )
        for event in second_events + third_events
        if isinstance(event, ToolCompleted)
    ] == ["x", "x"]

    # All inputs are notified in submission order on the shared stream.
    assert message_events == ["first", "second", "third"], message_events

    # The fused reply is delivered once to each central-stream subscriber.
    third_replies = [
        "".join(part.text for part in event.message.parts if isinstance(part, TextPart))
        for event in third_events
        if isinstance(event, AssistantCompleted)
    ]
    assert "handled first second and third" in third_replies, third_replies
    assert any(
        isinstance(event, AssistantCompleted)
        for event in second_events
    ), "central stream subscriber missed the merged reply"

    # Both queued messages were consumed by ONE model call after the fusion.
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_background_task_completion_reaches_tui_job_panel(foldin_app) -> None:
    """A completed background job must publish a terminal ``job_updated`` so
    the TUI task panel stops showing it as running.

    Regression: ``JobRegistry._finish`` only fired ``on_complete`` (a
    completion notice the TUI never applies); no terminal ``job_updated``
    reached live clients, so tasks stayed "running" forever."""

    from XBotv2.coretools.shell import shell_tools

    ctx = await foldin_app.state.manager.open_session(
        session_id="task-panel",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "hi"}]),
    )
    services = ctx.application._context
    services.permissions.replace_policies((PermissionPolicy(
        rules=(PermissionRule(tool_pattern=".*", decision="allow"),),
        default_decision="ask",
    ),))
    events = ctx.event_stream.subscribe()

    registry = services.jobs
    assert registry is not None
    tools = {
        tool.name: tool
        for tool in shell_tools(
            None,
            registry,
            str(foldin_app.state.paths.data_dir),
            services.artifacts,
        )
    }
    assert set(tools["shell"].parameters["properties"]) == {
        "command",
        "cwd",
        "background",
        "name",
        "sandbox_permissions",
        "justification",
    }
    started = await tools["shell"].ainvoke(
        {"command": "echo done", "background": True},
    )
    job_id = started.output.parts[0].text.removeprefix("Started ")
    await registry.wait([job_id])

    task_updates = []
    async with asyncio.timeout(1):
        while True:
            event = (await anext(events)).event
            if isinstance(event, JobUpdatedEvent):
                task_updates.append(event.view.state)
            if "succeeded" in task_updates:
                break
    assert "succeeded" in task_updates, task_updates
    assert "running" in task_updates, task_updates
    assert task_updates.index("running") < task_updates.index("succeeded"), task_updates


@pytest.mark.asyncio
async def test_background_job_failure_and_cancel_notices_are_consumed_once(
    foldin_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from XBotv2.coretools.shell import ShellCommandError, shell_tools

    llm = MockLLM(responses=[{"content": "acknowledged"}])
    ctx = await foldin_app.state.manager.open_session(
        session_id="job-terminal-states",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    services = ctx.application._context
    events = ctx.event_stream.subscribe()
    blocked_started = asyncio.Event()

    async def run(command: str, **_kwargs: object) -> str:
        if command == "fail":
            raise ShellCommandError("command failed", exit_code=7)
        if command == "block":
            blocked_started.set()
            await asyncio.Event().wait()
        return "completed"

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    tools = {
        tool.name: tool
        for tool in shell_tools(
            None,
            services.jobs,
            str(foldin_app.state.paths.data_dir),
            services.artifacts,
        )
    }

    async def start(command: str) -> str:
        result = await tools["shell"].ainvoke(
            {"command": command, "background": True},
        )
        return result.output.parts[0].text.removeprefix("Started ")

    failed_id = await start("fail")
    cancelled_id = await start("block")
    await blocked_started.wait()
    await services.jobs.cancel(cancelled_id)
    await services.jobs.wait([failed_id, cancelled_id], timeout=1)

    updates: dict[str, list[str]] = {failed_id: [], cancelled_id: []}
    completions: list[JobCompletedEvent] = []
    async with asyncio.timeout(1):
        while any(not states or states[-1] not in {
            "failed_running", "cancelled_running",
        } for states in updates.values()) or len(completions) < 2:
            event = (await anext(events)).event
            if isinstance(event, JobUpdatedEvent) and event.view.id in updates:
                updates[event.view.id].append(event.view.state)
            elif isinstance(event, JobCompletedEvent):
                completions.append(event)

    assert updates[failed_id][0] == "queued"
    assert updates[failed_id][-1] == "failed_running"
    assert updates[cancelled_id][0] == "queued"
    assert updates[cancelled_id][-1] == "cancelled_running"
    assert updates[failed_id].count("failed_running") == 1
    assert updates[cancelled_id].count("cancelled_running") == 1
    assert {
        item.view.id: item.view.state for item in completions
    } == {
        failed_id: "failed_running",
        cancelled_id: "cancelled_running",
    }
    assert len(ctx.engine.inbox) == 2
    assert llm.call_count == 0

    await asyncio.wait_for(
        _collect(_runtime_command(ctx, "review jobs", "review-jobs")),
        timeout=3,
    )
    assert llm.call_count == 1
    assert len(ctx.engine.inbox) == 0
    assert [message.kind for message in ctx.engine.messages] == [
        "runtime_notice", "runtime_notice", "human_input", "assistant",
    ]


@pytest.mark.asyncio
async def test_shell_output_pages_are_continuable_through_shell_tools(
    foldin_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from XBotv2.coretools.shell import shell_tools

    ctx = await foldin_app.state.manager.open_session(
        session_id="shell-output-pages",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "unused"}]),
    )
    jobs = ctx.application._context.jobs
    assert jobs is not None

    async def run(command: str, **_kwargs: object) -> str:
        return "" if command == "emit empty" else "abcdef"

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    tools = {
        tool.name: tool
        for tool in shell_tools(
            None,
            jobs,
            str(foldin_app.state.paths.data_dir),
            ctx.application._context.artifacts,
        )
    }
    started = await tools["shell"].ainvoke(
        {"command": "emit output", "background": True},
    )
    job_id = started.output.parts[0].text.removeprefix("Started ")
    await jobs.wait([job_id], timeout=1)
    job = jobs.get_or_none(job_id)
    assert job is not None and job.result is not None
    assert job.result.output_ref.kind.value == "tool_results"
    assert ctx.application._context.artifacts.read(job.result.output_ref) == b"abcdef"

    first = await tools["read_shell"].ainvoke(
        {"id": job_id, "cursor": None, "max_bytes": 3},
    )
    second = await tools["read_shell"].ainvoke(
        {"id": job_id, "cursor": 3, "max_bytes": 3},
    )
    assert isinstance(first, ToolSucceeded)
    assert isinstance(second, ToolSucceeded)
    assert json.loads(first.output.parts[0].text) == {
        "data": "abc",
        "next_cursor": 3,
        "eof": False,
        "truncated": True,
    }
    assert json.loads(second.output.parts[0].text) == {
        "data": "def",
        "next_cursor": None,
        "eof": True,
        "truncated": False,
    }

    empty_started = await tools["shell"].ainvoke(
        {"command": "emit empty", "background": True},
    )
    empty_id = empty_started.output.parts[0].text.removeprefix("Started ")
    await jobs.wait([empty_id], timeout=1)
    empty_page = await tools["read_shell"].ainvoke({"id": empty_id})
    assert isinstance(empty_page, ToolSucceeded)
    assert json.loads(empty_page.output.parts[0].text) == {
        "data": "",
        "next_cursor": None,
        "eof": True,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_session_close_cancels_and_removes_owned_background_job(
    foldin_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from XBotv2.coretools.shell import shell_tools

    ctx = await foldin_app.state.manager.open_session(
        session_id="job-session-close",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "unused"}]),
    )
    jobs = ctx.application._context.jobs
    assert jobs is not None
    command_started = asyncio.Event()
    command_cancelled = asyncio.Event()

    async def run(_command: str, **_kwargs: object) -> str:
        command_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            command_cancelled.set()
            raise

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    shell_tool = next(
        tool
        for tool in shell_tools(
            None,
            jobs,
            str(foldin_app.state.paths.data_dir),
            ctx.application._context.artifacts,
        )
        if tool.name == "shell"
    )
    result = await shell_tool.ainvoke(
        {"command": "block", "background": True},
    )
    job_id = result.output.parts[0].text.removeprefix("Started ")
    await command_started.wait()

    await foldin_app.state.manager.close_session(
        "job-session-close",
        reason="test_session_close",
    )

    assert command_cancelled.is_set()
    assert jobs.closing is True
    assert jobs.all() == []


@pytest.mark.asyncio
async def test_regenerate_publishes_the_replayed_input_once(foldin_app) -> None:
    runtime = await foldin_app.state.manager.open_session(
        session_id="regenerate-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[
            {"content": "first answer"},
            {"content": "replacement answer"},
        ]),
    )

    async def drain_turn(stream):
        events = []
        async for frame in stream:
            events.append(frame.event)
            if isinstance(frame.event, LoopTurnEnded):
                return events
        raise AssertionError("turn stream ended without a terminal event")

    first_stream = runtime.event_stream.subscribe()
    await runtime.send_message("original question", "original-request")
    await drain_turn(first_stream)
    await first_stream.aclose()

    replay_stream = runtime.event_stream.subscribe()
    await start_regenerate_turn(runtime, request_id="regenerate-request")
    replay_events = await drain_turn(replay_stream)
    await replay_stream.aclose()

    published_inputs = [
        event.record.root
        for event in replay_events
        if isinstance(event, MessagePublishedEvent)
    ]
    assert [(record.id, record.content) for record in published_inputs] == [
        ("original-request", "original question"),
    ]


@pytest.mark.asyncio
async def test_interrupted_turn_emits_one_cancel_terminal_and_runtime_recovers(
    foldin_app,
) -> None:
    runtime = await foldin_app.state.manager.open_session(
        session_id="interrupt-recovery",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[
            {
                "chunks": ["partial", " response"],
                "chunk_delay_ms": 500,
            },
            {"content": "recovered"},
        ]),
    )

    interrupted_stream = runtime.event_stream.subscribe()
    await runtime.send_message("interrupt me", "interrupt-request")
    assert runtime.request_interrupt()
    interrupted = await asyncio.wait_for(drain(interrupted_stream), timeout=2)
    await interrupted_stream.aclose()

    terminals = [
        event.outcome
        for event in interrupted
        if isinstance(event, LoopTurnEnded)
    ]
    assert terminals == [TurnCancelled(reason="client_interrupt")]
    assert not runtime.turn_lock.locked()
    assert runtime.engine.pending_input_count == 0

    recovery_stream = runtime.event_stream.subscribe()
    await runtime.send_message("try again", "recovery-request")
    recovered = await asyncio.wait_for(drain(recovery_stream), timeout=2)
    await recovery_stream.aclose()
    assert any(
        isinstance(event, AssistantCompleted)
        and any(
            isinstance(part, TextPart) and part.text == "recovered"
            for part in event.message.parts
        )
        for event in recovered
    )


async def drain(stream):
    events = []
    async for frame in stream:
        events.append(frame.event)
        if isinstance(frame.event, LoopTurnEnded):
            return events
    raise AssertionError("turn stream ended without a terminal event")
