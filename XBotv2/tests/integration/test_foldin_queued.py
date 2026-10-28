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

from XBotv2.agentloop.impl.tool_runtime import invoke_tool_function
import asyncio
import json
from pathlib import Path
import pytest
import pytest_asyncio
import yaml
import httpx
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.coretools.impl.shell import ShellJobResult
from XBotv2.agentloop.api.contracts import AGENT_LOOP, INBOX, TOOLS, AllTools
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.session.api.contracts import OpenSession, THREAD_EVENTS
from XBotv2.session.api.snapshot import thread_snapshot
from XBotv2.permissions.api.models import PERMISSIONS
from XBotv2.jobs.api.contracts import JOBS, JobRegistryClosed
from XBotv2.core.api.tools import Tool, ToolSucceeded
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.loader.api.contracts import PluginOverlay
from XBotv2.agentloop.api.outputs import (
    AssistantCompleted,
    AssistantTextDelta,
    LoopError,
    LoopTurnEnded,
    TurnCancelled,
    ToolCompleted,
)
from XBotv2.core.api.parts import TextPart
from XBotv2.jobs.api.protocol import JobCompletedEvent, JobUpdatedEvent
from XBotv2.session.api.events import MessagePublishedEvent
from XBotv2.usage.api.events import UsageUpdated


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
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await open_runtime(
        paths=RuntimePaths.from_data_dir(data_dir),
        overrides=PluginOverlay.parse([{"id": "session", "config": {
            "provider_name": "default", "workspace_root": str(workspace), "no_plugins": True,
        }}]),
    )
    server.server.state.manager = server.sessions
    server.server.state.paths = server.runtime_paths
    try:
        yield server.server
    finally:
        await server.close()


async def _open(manager, *, llm_override=None, mode="new", **kwargs):
    return await manager.open(OpenSession(mode=mode, model_override=llm_override, **kwargs))


async def _collect(stream):
    return [event async for event in stream]


def _runtime_command(runtime, content: str, request_id: str, *, delivery="steer"):
    """Issue a command while consuming the central session event stream."""
    async def stream():
        events = runtime.require(THREAD_EVENTS).subscribe()
        try:
            await runtime.require(AGENT_LOOP).send_message(content, request_id, delivery=delivery)
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
async def test_thread_snapshot_without_optional_plugins(foldin_app):
    runtime = await _open(foldin_app.state.manager,
        session_id="snapshot",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "ok"}]),
    )

    snapshot = await asyncio.wait_for(thread_snapshot(runtime), timeout=2)
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
    ctx = await _open(app.state.manager,
        session_id="foldin",
        thread_id="t",
        provider_name="default",
        workspace_root=str(app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.require(PERMISSIONS).grant_once("wait_for_release", {})
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.require(TOOLS).register(Tool.from_function(wait_for_release), owner=ctx)
    ctx.require(TOOLS).restrict(AllTools())

    ev_stream = ctx.require(THREAD_EVENTS).subscribe()

    async def _collect_events(stream):
        return [event async for event in stream]

    first_task = asyncio.create_task(
        _collect_events(_runtime_command(ctx, "first request", "req-1"))
    )
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        busy = await client.post(
            "/sessions/foldin/threads/t/history/regenerate",
            json={"request_id": "busy-regenerate"},
        )
    assert busy.status_code == 409
    assert busy.json()["code"] == "thread_busy"
    assert busy.json()["retryable"] is True
    second_task = asyncio.create_task(
        _collect_events(_runtime_command(ctx, "second queued", "req-2"))
    )
    async with asyncio.timeout(3):
        while ctx.require(AGENT_LOOP).pending_input_count != 1:
            await asyncio.sleep(0.001)
    assert ctx.require(AGENT_LOOP).pending_input_count == 1
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

    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_default_steer_is_wrapped_only_for_model_and_survives_resume(foldin_app):
    from XBotv2.core.api.messages import HumanInputMessage, ToolMessage
    from XBotv2.core.api.provider import ProviderUser
    from XBotv2.agentloop.api.outputs import TurnCancelled

    llm = MockLLM(responses=[
        {"tool_calls": [{"id": "wait-steer", "name": "wait_for_release", "args": {"value": "finished"}}]},
        {"content": "continued current task"},
    ])
    first_events, _, _ = await _run_foldin(foldin_app, llm)
    runtime = await foldin_app.state.manager.locate("foldin", "t")
    messages = runtime.require(LOOP_STATE).messages
    human = [message for message in messages if isinstance(message, HumanInputMessage)]
    assert [message.steering for message in human] == [False, True]
    assert [message.parts for message in human] == [
        (TextPart(text="first request"),), (TextPart(text="second queued"),),
    ]
    assert not any(isinstance(event, TurnCancelled) for event in first_events)
    assert all(isinstance(message.outcome, ToolSucceeded) for message in messages if isinstance(message, ToolMessage))

    def assert_model_wrapper(request):
        ordinary = next(
            message for message in request.messages
            if isinstance(message, ProviderUser) and TextPart(text="first request") in message.parts
        )
        assert ordinary.parts == (TextPart(text="first request"),)
        steered = next(
            message for message in request.messages
            if isinstance(message, ProviderUser) and TextPart(text="second queued") in message.parts
        )
        assert len(steered.parts) == 2
        instruction = steered.parts[0].text.lower()
        assert "temporary" in instruction and "unless" in instruction
        assert "interrupt" in instruction

    assert_model_wrapper(llm.request_history[-1])
    await foldin_app.state.manager.close_session("foldin")
    resumed_llm = MockLLM(responses=[{"content": "resumed"}])
    resumed = await _open(foldin_app.state.manager,
        session_id="foldin", thread_id="t", mode="resume", llm_override=resumed_llm,
        provider_name="default", workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
    )
    resumed_events = await _collect(_runtime_command(resumed, "next request", "after-resume"))
    assert not any(isinstance(event, LoopError) for event in resumed_events)
    assert_model_wrapper(resumed_llm.request_history[-1])
    assert [
        message.steering for message in resumed.require(LOOP_STATE).messages
        if isinstance(message, HumanInputMessage)
    ] == [False, True, False]


async def _run_multi_queue(app, llm):
    """First turn blocks on a tool; SECOND and THIRD are queued meanwhile."""
    ctx = await _open(app.state.manager,
        session_id="multi",
        thread_id="t",
        provider_name="default",
        workspace_root=str(app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.require(PERMISSIONS).grant_once("wait_for_release", {})
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.require(TOOLS).register(Tool.from_function(wait_for_release), owner=ctx)
    ctx.require(TOOLS).restrict(AllTools())

    async def collect(stream):
        events = []
        async for event in stream:
            events.append(event)
        return events

    ev_stream = ctx.require(THREAD_EVENTS).subscribe()
    first_task = asyncio.create_task(collect(_runtime_command(ctx, "first", "req-1")))
    await asyncio.wait_for(tool_started.wait(), timeout=3)
    second_task = asyncio.create_task(collect(_runtime_command(ctx, "second", "req-2")))
    third_task = asyncio.create_task(collect(_runtime_command(ctx, "third", "req-3")))
    async with asyncio.timeout(3):
        while ctx.require(AGENT_LOOP).pending_input_count != 2:
            await asyncio.sleep(0.001)
    assert ctx.require(AGENT_LOOP).pending_input_count == 2
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
    all subscribers see the single merged reply.

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

    from XBotv2.coretools.impl.shell import shell_tools

    ctx = await _open(foldin_app.state.manager,
        session_id="task-panel",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "hi"}]),
    )
    events = ctx.require(THREAD_EVENTS).subscribe()

    registry = ctx.require(JOBS)
    assert registry is not None
    tools = {
        tool.name: tool
        for tool in shell_tools(
            None,
            registry,
            str(foldin_app.state.paths.data_dir),
            ctx.require("artifacts"),
        )
    }
    assert set(tools["shell"].parameters["properties"]) == {
        "command",
        "cwd",
        "background",
        "name",
        "sandbox_permissions",
        "justification",
        "timeout_seconds",
    }
    started = await invoke_tool_function(tools["shell"],
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
    from XBotv2.coretools.impl.shell import ShellCommandError, shell_tools

    llm = MockLLM(responses=[{"content": "acknowledged"}])
    ctx = await _open(foldin_app.state.manager,
        session_id="job-terminal-states",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    events = ctx.require(THREAD_EVENTS).subscribe()
    blocked_started = asyncio.Event()

    async def run(command: str, **_kwargs: object) -> ShellJobResult:
        if command == "fail":
            raise ShellCommandError("command failed", exit_code=7)
        if command == "block":
            blocked_started.set()
            await asyncio.Event().wait()
        ref = await _kwargs["artifacts"].put(ArtifactKind.TOOL_RESULT, b"completed")
        return ShellJobResult(ref, 0)

    monkeypatch.setattr("XBotv2.coretools.impl.shell.run_shell_command", run)
    tools = {
        tool.name: tool
        for tool in shell_tools(
            None,
            ctx.require(JOBS),
            str(foldin_app.state.paths.data_dir),
            ctx.require("artifacts"),
        )
    }

    async def start(command: str) -> str:
        result = await invoke_tool_function(tools["shell"],
            {"command": command, "background": True},
        )
        return result.output.parts[0].text.removeprefix("Started ")

    failed_id = await start("fail")
    cancelled_id = await start("block")
    await blocked_started.wait()
    await ctx.require(JOBS).cancel(cancelled_id)
    await ctx.require(JOBS).wait([failed_id, cancelled_id], timeout=1)

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
    assert len(ctx.require(INBOX)) == 2
    assert llm.call_count == 0

    await asyncio.wait_for(
        _collect(_runtime_command(ctx, "review jobs", "review-jobs")),
        timeout=3,
    )
    assert llm.call_count == 1
    assert len(ctx.require(INBOX)) == 0
    assert [message.kind for message in ctx.require(LOOP_STATE).messages] == [
        "runtime_notice", "runtime_notice", "human_input", "assistant",
    ]


@pytest.mark.asyncio
async def test_shell_output_pages_are_continuable_through_shell_tools(
    foldin_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from XBotv2.coretools.impl.shell import shell_tools

    ctx = await _open(foldin_app.state.manager,
        session_id="shell-output-pages",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "unused"}]),
    )
    jobs = ctx.require(JOBS)
    assert jobs is not None

    async def run(command: str, **_kwargs: object) -> ShellJobResult:
        payload = b"" if command == "emit empty" else b"abcdef"
        ref = await _kwargs["artifacts"].put(ArtifactKind.TOOL_RESULT, payload)
        return ShellJobResult(ref, 0)

    monkeypatch.setattr("XBotv2.coretools.impl.shell.run_shell_command", run)
    tools = {
        tool.name: tool
        for tool in shell_tools(
            None,
            jobs,
            str(foldin_app.state.paths.data_dir),
            ctx.require("artifacts"),
        )
    }
    started = await invoke_tool_function(tools["shell"],
        {"command": "emit output", "background": True},
    )
    job_id = started.output.parts[0].text.removeprefix("Started ")
    empty_started = await invoke_tool_function(tools["shell"],
        {"command": "emit empty", "background": True},
    )
    empty_id = empty_started.output.parts[0].text.removeprefix("Started ")
    waited = await jobs.wait([job_id, empty_id], mode="all", timeout=1)
    assert waited.reason == "ready" and not waited.pending
    job = jobs.get_or_none(job_id)
    assert job is not None and job.result is not None
    assert job.result.output_ref.kind.value == "tool_results"
    assert (await ctx.require("artifacts").read(job.result.output_ref)) == b"abcdef"

    first = await invoke_tool_function(tools["read_shell"],
        {"id": job_id, "cursor": None, "max_bytes": 3},
    )
    second = await invoke_tool_function(tools["read_shell"],
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

    empty_page = await invoke_tool_function(tools["read_shell"], {"id": empty_id})
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
    from XBotv2.coretools.impl.shell import shell_tools

    ctx = await _open(foldin_app.state.manager,
        session_id="job-session-close",
        thread_id="t",
        provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "unused"}]),
    )
    jobs = ctx.require(JOBS)
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

    monkeypatch.setattr("XBotv2.coretools.impl.shell.run_shell_command", run)
    shell_tool = next(
        tool
        for tool in shell_tools(
            None,
            jobs,
            str(foldin_app.state.paths.data_dir),
            ctx.require("artifacts"),
        )
        if tool.name == "shell"
    )
    result = await invoke_tool_function(shell_tool,
        {"command": "block", "background": True},
    )
    job_id = result.output.parts[0].text.removeprefix("Started ")
    await command_started.wait()

    await foldin_app.state.manager.close_session(
        "job-session-close",
    )

    assert command_cancelled.is_set()
    assert jobs.closing is True
    with pytest.raises(JobRegistryClosed):
        jobs.all()


@pytest.mark.asyncio
async def test_regenerate_publishes_the_replayed_input_once(foldin_app) -> None:
    from XBotv2.session.api.records import project_message

    runtime = await _open(foldin_app.state.manager,
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

    first_stream = runtime.require(THREAD_EVENTS).subscribe()
    await runtime.require(AGENT_LOOP).send_message("original question", "original-request")
    await drain_turn(first_stream)
    await first_stream.aclose()

    replay_stream = runtime.require(THREAD_EVENTS).subscribe()
    await runtime.require(AGENT_LOOP).regenerate("regenerate-request")
    replay_events = await drain_turn(replay_stream)
    await replay_stream.aclose()

    published_inputs = [
        event.record.root
        for event in replay_events
        if isinstance(event, MessagePublishedEvent)
    ]
    assert [(record.id, record.content) for record in published_inputs] == [
        ("regenerate-request", "original question"),
    ]
    assert published_inputs == [
        project_message(message) for message in runtime.require(LOOP_STATE).messages
        if message.kind == "human_input"
    ]


@pytest.mark.asyncio
async def test_interrupted_turn_emits_one_cancel_terminal_and_runtime_recovers(
    foldin_app,
) -> None:
    runtime = await _open(foldin_app.state.manager,
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

    interrupted_stream = runtime.require(THREAD_EVENTS).subscribe()
    await runtime.require(AGENT_LOOP).send_message("interrupt me", "interrupt-request")
    async with asyncio.timeout(3):
        async for frame in interrupted_stream:
            if isinstance(frame.event, AssistantTextDelta):
                break
    assert await runtime.require(AGENT_LOOP).interrupt()
    interrupted = await asyncio.wait_for(drain(interrupted_stream), timeout=2)
    await interrupted_stream.aclose()

    terminals = [
        event.outcome
        for event in interrupted
        if isinstance(event, LoopTurnEnded)
    ]
    assert terminals == [TurnCancelled(reason="client_interrupt")]
    assert not runtime.require(AGENT_LOOP).busy
    assert runtime.require(AGENT_LOOP).pending_input_count == 0

    recovery_stream = runtime.require(THREAD_EVENTS).subscribe()
    await runtime.require(AGENT_LOOP).send_message("try again", "recovery-request")
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

@pytest.mark.asyncio
async def test_close_before_turn_start_preserves_runtime_input_without_terminal(foldin_app):
    from XBotv2.agentloop.api.contracts import InboxItem, InboxTarget, RuntimeInput
    from XBotv2.agentloop.api.events import Events
    from XBotv2.agentloop.api.storage import ARCHIVE
    from XBotv2.core.api.messages import RuntimeNoticeMessage

    manager = foldin_app.state.manager
    options = dict(
        session_id="pre-start-close", thread_id="t", provider_name="default",
        workspace_root=str(foldin_app.state.paths.data_dir), no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "done"}]),
    )
    runtime = await _open(manager, **options)
    entered = asyncio.Event()

    async def block_input(event):
        entered.set()
        await asyncio.Event().wait()

    runtime.on(Events.ON_TURN_INPUT, block_input)
    stream = runtime.require(THREAD_EVENTS).subscribe()

    async def collect():
        return [frame.event async for frame in stream]

    frames = asyncio.create_task(collect())
    await runtime.require(AGENT_LOOP).submit_input(
        InboxItem(
            id="runtime-before-start", target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(source="test", event="notice", content="preserved"),
        ),
        wake=False,
    )
    assert runtime.require(AGENT_LOOP).resume_pending_inputs()
    await asyncio.wait_for(entered.wait(), 3)
    await manager.close_session("pre-start-close", reason="client_disconnected")
    events = await asyncio.wait_for(frames, 3)
    assert not any(isinstance(event, LoopTurnEnded) for event in events)

    rollout = foldin_app.state.paths.session("pre-start-close").thread("t").rollout_file
    records = [json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines()]
    assert any(
        record["kind"] == "input_accepted"
        and record["data"]["item"]["id"] == "runtime-before-start"
        for record in records
    )
    assert not any(record["kind"] == "input_committed" for record in records)

    options["llm_override"] = MockLLM(responses=[{"content": "done"}])
    resumed = await _open(manager, mode="resume", **options)
    async with asyncio.timeout(5):
        while resumed.require(AGENT_LOOP).busy or resumed.require(AGENT_LOOP).pending_input_count:
            await asyncio.sleep(0.001)
    messages = await resumed.require(ARCHIVE).surface("pre-start-close", "t")
    assert sum(
        isinstance(item, RuntimeNoticeMessage) and item.id == "runtime-before-start"
        for item in messages
    ) == 1


@pytest.mark.asyncio
async def test_queued_user_and_explicit_runtime_wakes_drain_in_order(foldin_app):
    """Explicit wake requests survive coalescing while a turn owns the lock."""
    from XBotv2.agentloop.api.contracts import InboxItem, RuntimeInput
    from XBotv2.agentloop.api.outputs import ExecutionEnded
    from XBotv2.core.api.domain import InboxTarget

    llm = MockLLM(responses=[
        {"tool_calls": [{"id": "wait", "name": "wait_for_release", "args": {"value": "x"}}]},
        *({"content": f"answer {index}"} for index in range(5)),
    ])
    ctx = await _open(
        foldin_app.state.manager, session_id="queue-wakes", thread_id="t",
        provider_name="default", workspace_root=str(foldin_app.state.paths.data_dir),
        no_plugins=True, llm_override=llm,
    )
    loop = ctx.require(AGENT_LOOP)
    started, release = asyncio.Event(), asyncio.Event()

    async def wait_for_release(value: str) -> str:
        started.set()
        await release.wait()
        return value

    ctx.require(PERMISSIONS).grant_once("wait_for_release", {})
    ctx.require(TOOLS).register(Tool.from_function(wait_for_release), owner=ctx)
    ctx.require(TOOLS).restrict(AllTools())
    stream = ctx.require(THREAD_EVENTS).subscribe()
    try:
        await loop.send_message("first", "first")
        await asyncio.wait_for(started.wait(), 5)
        await loop.send_message("queued one", "q1", delivery="queue")
        await loop.send_message("queued two", "q2", delivery="queue")
        for identity in ("runtime-1", "runtime-2"):
            await loop.submit_input(InboxItem(
                id=identity, target=InboxTarget.NEXT_TURN,
                input=RuntimeInput(source="test", event="task", content=identity),
            ), wake=True)
        release.set()
        completed = []
        async with asyncio.timeout(10):
            while len(completed) < 5:
                event = (await anext(stream)).event
                if isinstance(event, ExecutionEnded):
                    completed.append(event.execution.input_ids)
        assert completed == [("first",), ("q1",), ("q2",), ("runtime-1",), ("runtime-2",)]
        assert llm.call_count == 6
        assert loop.pending_input_count == 0
    finally:
        release.set()
        await stream.aclose()
