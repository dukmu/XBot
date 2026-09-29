"""End-to-end checks for the loop-owned input path."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from XBotv2.agentloop.contracts import HumanInput, InboxItem, RuntimeInput
from XBotv2.core.domain import InboxTarget
from XBotv2.session.records import (
    AssistantRecord,
    HumanInputRecord,
    RuntimeNoticeRecord,
    ToolRecord,
    project_message,
)
from XBotv2.core.tools import ToolFailed, ToolSucceeded
from XBotv2.session.runtime import SessionRuntime
from XBotv2.session.contracts import conversation_replay
from XBotv2.application.app import create_agent_application, start_application
from XBotv2.application.host import mounted_application
from XBotv2.core.messages import AssistantMessage
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.filesystem.session_lock import acquire_session
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderUser
from XBotv2.core.tools import Tool
from XBotv2.permissions import PermissionPolicy, PermissionRule
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.plugin import thread_persistence_factory
from XBotv2.session.manager import SessionManager


def _record_text(record):
    if isinstance(record, (AssistantRecord, HumanInputRecord, RuntimeNoticeRecord)):
        return record.content
    if isinstance(record, ToolRecord) and isinstance(record.outcome, ToolSucceeded):
        return "".join(part.text for part in record.outcome.output.parts)
    return ""


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("short_circuit", ["reject", "complete"])
async def test_short_circuited_batch_preserves_unprocessed_user_input(tmp_path, persistent, short_circuit):
    from XBotv2.agentloop.events import CompleteTurn, Events, RejectInput
    from XBotv2.agentloop.outputs import LoopError

    provider = MockLLM(responses=[{"content": "answered"}])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), workspace_root=tmp_path,
        no_plugins=True, llm_override=provider,
        extra_plugins=[{"id": "persistence", "disabled": not persistent}],
    )
    notice = InboxItem(
        id="rejected-notice", target=InboxTarget.NEXT_STEP,
        input=RuntimeInput(source="job", event="completion", content="ignore this notice"),
    )
    user = InboxItem(
        id="unprocessed-user", target=InboxTarget.NEXT_TURN,
        input=HumanInput(content="answer this question"),
    )

    def reject_notice(event):
        if event.input.id == notice.id:
            if short_circuit == "complete":
                return CompleteTurn(result=LoopError(code="input_handled", message="notice handled"))
            return RejectInput(error="notice rejected")

    try:
        application.on(Events.ON_TURN_INPUT, reject_notice)
        await application.engine.submit_input(InboxItem(
            id="accepted-notice", target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(source="job", event="completion", content="accepted notice"),
        ), wake=False)
        await application.engine.submit_input(notice, wake=False)
        events = [event async for event in application.engine.run_turn(user)]
        assert any(event.kind == "error" for event in events)
        assert provider.call_count == 0
        assert [message.id for message in application.engine.messages] == ["accepted-notice"]
        assert [item.id for item in application.engine.pending_inputs] == [user.id]
        if persistent:
            assert [item.id for item in application.thread_persistence.inbox.load()] == [user.id]
        retried = [event async for event in application.engine.run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert application.engine.pending_input_count == 0
        assert provider.call_count == 1
        assert [message.id for message in application.engine.messages if message.kind == "human_input"] == [user.id]
    finally:
        await application.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("short_circuit", ["reject", "complete"])
@pytest.mark.parametrize("with_tool", [False, True])
async def test_step_short_circuit_is_published_and_leaves_suffix_pending(tmp_path, persistent, short_circuit, with_tool):
    from XBotv2.agentloop.events import CompleteTurn, Events, RejectInput
    from XBotv2.agentloop.outputs import LoopError

    first_response = (
        {"tool_calls": [{"id": "call-1", "name": "observe", "args": {}}]}
        if with_tool else {"content": "first"}
    )
    provider = MockLLM(responses=[first_response, {"content": "follow-up"}])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), workspace_root=tmp_path,
        no_plugins=True, llm_override=provider,
        extra_plugins=[{"id": "persistence", "disabled": not persistent}],
    )
    items = [InboxItem(
        id=identity, target=InboxTarget.NEXT_STEP, input=HumanInput(content=identity),
    ) for identity in ("accepted-prefix", "handled-input", "pending-suffix")]

    async def enqueue(_event):
        dispose()
        for item in items:
            await application.engine.submit_input(item, wake=False)

    def handle(event):
        if event.input.id == "handled-input":
            if short_circuit == "reject":
                return RejectInput(error="rejected steering")
            return CompleteTurn(result=LoopError(code="handled", message="handled steering"))

    try:
        async def observe() -> str:
            """Return a deterministic tool result."""
            return "observed"

        application.engine.tools.register(Tool.from_function(observe), cleanup="caller")
        application.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern="observe", decision="allow"),
        )),))
        dispose = application.on(Events.MODEL_REQUEST_READY, enqueue)
        application.on(Events.ON_TURN_INPUT, handle)
        events = [event async for event in application.engine.run_turn(InboxItem(
            id="initial", target=InboxTarget.NEXT_TURN, input=HumanInput(content="start"),
        ))]
        dispose()
        expected_code = "user_message_rejected" if short_circuit == "reject" else "handled"
        assert any(event.kind == "error" and event.code == expected_code for event in events)
        assert provider.call_count == 1
        assert sum(event.kind == "tool_completed" for event in events) == int(with_tool)
        assert sum(event.kind == "turn_ended" for event in events) == 1
        assert [item.id for item in application.engine.pending_inputs] == ["pending-suffix"]
        assert [message.id for message in application.engine.messages if message.kind == "human_input"] == [
            "initial", "accepted-prefix",
        ]
        if persistent:
            assert [item.id for item in application.thread_persistence.inbox.load()] == ["pending-suffix"]
        retried = [event async for event in application.engine.run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert provider.call_count == 2
        assert application.engine.pending_input_count == 0
    finally:
        await application.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_notice", [False, True])
@pytest.mark.parametrize("hook", ["accept", "canonicalize"])
async def test_input_hooks_cannot_break_consumption_identity(tmp_path, runtime_notice, hook):
    from XBotv2.agentloop.events import AcceptInput, Events, InputAccepted

    provider = MockLLM(responses=[{"content": "retried"}])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), workspace_root=tmp_path,
        no_plugins=True, llm_override=provider,
    )
    payload = RuntimeInput(source="job", event="done", content="result") if runtime_notice else HumanInput(content="question")
    item = InboxItem(id="original-input", target=InboxTarget.NEXT_TURN, input=payload)

    def replace_identity(event):
        if hook == "accept":
            return AcceptInput(event.input.model_copy(update={"id": "different-input"}))
        return InputAccepted(event.input, event.message.model_copy(update={"id": "different-input"}))

    try:
        dispose = application.on(
            Events.ON_TURN_INPUT if hook == "accept" else Events.INPUT_ACCEPTED,
            replace_identity,
        )
        events = [event async for event in application.engine.run_turn(item)]
        dispose()
        assert any(event.kind == "error" and "identity" in event.message for event in events)
        assert provider.call_count == 0
        assert application.engine.messages.snapshot() == ()
        assert application.thread_persistence.history.load_surface() == ()
        assert application.thread_persistence.inbox.load() == [item]
        retried = [event async for event in application.engine.run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert application.engine.messages[0].id == item.id
        assert application.engine.pending_input_count == 0
    finally:
        await application.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["compact", "clear"])
async def test_resume_reconciles_inbox_against_trace_not_current_surface(tmp_path, operation):
    from XBotv2.core.messages import CompactionSummaryMessage
    from XBotv2.persistence.store import ThreadPersistence

    paths = RuntimePaths.from_data_dir(tmp_path / "data")

    async def start():
        return await start_application(
            paths=paths, workspace_root=tmp_path, session_id="reconcile-trace",
            thread_id="main", no_plugins=True,
            llm_override=MockLLM(responses=[{"content": "done"}]),
        )

    notice = InboxItem(
        id="committed-notice", target=InboxTarget.NEXT_STEP,
        input=RuntimeInput(source="job", event="completion", content="old notice"),
    )
    user = InboxItem(
        id="committed-user", target=InboxTarget.NEXT_TURN, input=HumanInput(content="old input"),
    )
    pending = InboxItem(
        id="unprocessed", target=InboxTarget.NEXT_TURN, input=HumanInput(content="new input"),
    )
    first = await start()
    try:
        await first.engine.submit_input(notice, wake=False)
        async for _event in first.engine.run_turn(user):
            pass
    finally:
        await first.destroy()

    store = ThreadPersistence.open(paths.session("reconcile-trace"), thread_id="main")
    # Model the crash boundary: history committed, inbox inputs not retired.
    # A subsequent surface replacement must not erase that commit evidence.
    from XBotv2.agentloop.contracts import Inserted
    for item in (notice, user, pending):
        store.inbox.append(Inserted(item=item, wake=False))
    source_ids = tuple(message.id for message in store.history.load_surface())
    replacement = (CompactionSummaryMessage(id="summary", summary="prior context"),) if operation == "compact" else ()
    store.history.replace_surface(
        source_ids, replacement, operation=operation, preserve_transcript=operation == "compact",
    )
    resumed = await start()
    try:
        assert [item.id for item in resumed.engine.pending_inputs] == [pending.id]
        assert [item.id for item in store.inbox.load()] == [pending.id]
        events = [event async for event in resumed.engine.run_pending()]
        assert not any(event.kind == "error" for event in events)
        assert resumed.engine.pending_input_count == 0
        assert store.inbox.load() == []
    finally:
        await resumed.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_live_human_input_is_projected_from_accepted_history(tmp_path, rejected):
    from XBotv2.agentloop.events import Events, RejectInput
    from XBotv2.session.events import InputConsumedEvent, MessagePublishedEvent

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    context = await start_application(
        paths=paths, workspace_root=tmp_path,
        llm_override=MockLLM(responses=[{"content": "reply"}]),
        extra_plugins=[
            {"id": "caption", "disabled": True},
            {"id": "content_cache", "config": {
                "threshold_chars": 100, "preview_chars": 20, "tail_chars": 5,
            }},
        ],
    )
    application = await mounted_application(context)
    runtime = SessionRuntime(paths, False, application, context.engine)
    events = runtime.event_stream.subscribe()
    if rejected:
        context.on(Events.ON_TURN_INPUT, lambda _event: RejectInput(error="rejected"))
    try:
        original = "long user input " * 100
        async for _event in context.engine.run_turn(InboxItem(
            id="request-identity", target=InboxTarget.NEXT_TURN,
            input=HumanInput(content=original),
        )):
            pass
        frames = [
            await asyncio.wait_for(anext(events), timeout=1)
            for _ in range(runtime.event_stream.sequence)
        ]
        published = [
            frame.event.record.root for frame in frames
            if isinstance(frame.event, MessagePublishedEvent)
        ]
        history = [
            project_message(message) for message in context.thread_persistence.history.load_surface()
            if message.kind == "human_input"
        ]
        assert published == history
        consumed_at = next(
            index for index, frame in enumerate(frames)
            if isinstance(frame.event, InputConsumedEvent)
        )
        assert all(
            index < consumed_at for index, frame in enumerate(frames)
            if isinstance(frame.event, MessagePublishedEvent)
        )
        if not rejected:
            assert len(history) == 1
            assert history[0].id == "request-identity"
            assert history[0].artifacts
            assert history[0].content != original
    finally:
        await events.aclose()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("cancelled", [True, False])
async def test_failed_step_input_is_released_for_retry(tmp_path, persistent, cancelled):
    from XBotv2.agentloop.events import Events
    from XBotv2.core.messages import HumanInputMessage

    application = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        workspace_root=tmp_path, no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "first"}, {"content": "retried"}]),
        extra_plugins=[{"id": "persistence", "disabled": not persistent}],
    )
    steer = InboxItem(
        id="steering", target=InboxTarget.NEXT_STEP, input=HumanInput(content="correction"),
    )

    async def enqueue_while_running(_event):
        await application.engine.submit_input(steer, wake=False)

    accepting = asyncio.Event()

    async def fail_steering(event):
        if event.input.id == steer.id:
            accepting.set()
            if cancelled:
                await asyncio.Event().wait()
            raise RuntimeError("steering handler failed")

    async def run():
        return [event async for event in application.engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN, input=HumanInput(content="start"),
        ))]

    task = None
    try:
        dispose_enqueue = application.on(Events.MODEL_REQUEST_READY, enqueue_while_running)
        dispose_failure = application.on(Events.ON_TURN_INPUT, fail_steering)
        task = asyncio.create_task(run())
        await asyncio.wait_for(accepting.wait(), timeout=5)
        if cancelled:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            events = await task
            assert any(event.kind == "error" and event.message == "steering handler failed" for event in events)
        dispose_enqueue()
        dispose_failure()
        assert [item.id for item in application.engine.inbox.pending] == [steer.id]
        retried = [event async for event in application.engine.run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert sum(
            isinstance(message, HumanInputMessage) and message.id == steer.id
            for message in application.engine.messages
        ) == 1
        if persistent:
            assert application.thread_persistence.inbox.load() == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await application.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
async def test_partial_input_batch_does_not_replay_committed_notice(tmp_path, persistent):
    from XBotv2.agentloop.events import Events
    from XBotv2.core.messages import RuntimeNoticeMessage
    from XBotv2.session.events import MessagePublishedEvent

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    application = await start_application(
        paths=paths,
        workspace_root=tmp_path, no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "continued"}]),
        extra_plugins=[{"id": "persistence", "disabled": not persistent}],
    )
    runtime = SessionRuntime(paths, False, await mounted_application(application), application.engine)
    stream = runtime.event_stream.subscribe()
    notice = InboxItem(
        id="completed-job", target=InboxTarget.NEXT_STEP,
        input=RuntimeInput(source="job", event="completion", content="job finished"),
    )
    user = InboxItem(id="followup", target=InboxTarget.NEXT_TURN, input=HumanInput(content="continue"))

    def fail_human_input(event):
        if event.input.id == user.id:
            raise RuntimeError("input handler failed")

    try:
        await application.engine.submit_input(notice, wake=False)
        dispose = application.on(Events.ON_TURN_INPUT, fail_human_input)
        failed = [event async for event in application.engine.run_turn(user)]
        assert any(event.kind == "error" for event in failed)
        dispose()
        assert [item.id for item in application.engine.inbox.pending] == [user.id]
        frames = [
            await asyncio.wait_for(anext(stream), timeout=1)
            for _ in range(runtime.event_stream.sequence)
        ]
        assert [
            frame.event.record.root for frame in frames
            if isinstance(frame.event, MessagePublishedEvent)
        ] == [project_message(application.engine.messages[0])]
        retried = [event async for event in application.engine.run_pending()]
        assert not any(event.kind == "error" for event in retried)
        messages = application.engine.messages
        assert sum(isinstance(message, RuntimeNoticeMessage) for message in messages) == 1
        assert application.engine.pending_input_count == 0
        if persistent:
            stored = application.thread_persistence.history.load_surface()
            assert sum(isinstance(message, RuntimeNoticeMessage) for message in stored) == 1
            assert application.thread_persistence.inbox.load() == []
    finally:
        await stream.aclose()
        await runtime.close()


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

    # The caption plugin auto-titles the first user message with its own
    # model call, so the mock must serve that call plus the turn's two.
    provider = MockLLM(responses=[
        {"content": "session title"},
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
    services.permissions.replace_policies((PermissionPolicy(rules=(
        PermissionRule(tool_pattern=".*", decision="allow"),
    )),))
    engine.tools.register(Tool.from_function(blocker), cleanup="caller")
    application = await mounted_application(services)
    runtime = SessionRuntime(paths, False, application, engine)

    async def collect(content: str, request_id: str):
        events = runtime.event_stream.subscribe()
        await runtime.send_message(content, request_id)
        collected = []
        async for frame in events:
            collected.append(frame.event)
            if frame.event.kind == "turn_ended":
                break
        await events.aclose()
        return collected

    first = asyncio.create_task(collect("first", "first-id"))
    await started.wait()
    second = asyncio.create_task(collect("steer", "steer-id"))
    await asyncio.sleep(0)
    release.set()
    first_events, second_events = await asyncio.gather(first, second)

    assert [_record_text(project_message(message)) for message in engine.messages] == [
        "first", "", "done", "steer", "merged reply",
    ]
    assert engine.pending_input_count == 0
    assert any(event.kind == "message" for event in first_events)
    assert second_events[-1].kind == "turn_ended"


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
    application = await mounted_application(services)
    runtime = SessionRuntime(paths, False, application, engine)
    await engine.submit_input(
        InboxItem(
            id="job-1",
            target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(
                source="job", event="completion", content="job finished",
            ),
        ),
        wake=False,
    )

    assert engine.pending_input_count == 1
    assert runtime.wakeup_task is None

    resumed_services = await start_application(
        paths=paths,
        session_id="durable-inbox",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        # caption auto-titles this session too: serve it before the turn.
        llm_override=MockLLM(responses=[
            {"content": "session title"},
            {"content": "observed"},
        ]),
    )
    resumed = resumed_services.engine
    assert resumed.pending_input_count == 1
    events = [
        event
        async for event in resumed.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="continue"),
            ),
            request_id="user-1",
        )
    ]
    assert resumed.pending_input_count == 0
    assert [_record_text(project_message(message)) for message in resumed.messages] == [
        "job finished", "continue", "observed",
    ]
    replayed = conversation_replay(resumed.messages)
    assert replayed[0].kind == "runtime_notice"
    assert replayed[0].source == "job"
    assert replayed[0].event == "completion"
    assert project_message(resumed.messages[1]).kind == "human_input"
    assert events[-1].kind == "turn_ended"
    await resumed_services.stop()
    await services.stop()


@pytest.mark.asyncio
async def test_edited_pending_input_is_hydrated_by_application_factory(
    temp_data_dir,
    temp_workspace,
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await start_application(
        paths=paths,
        session_id="edited-durable-inbox",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    resumed_services = None
    try:
        engine = services.engine
        await engine.submit_input(
            InboxItem(
                id="keep",
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="draft"),
            ),
            wake=False,
        )
        await engine.submit_input(
            InboxItem(
                id="remove",
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="discard this"),
            ),
            wake=False,
        )
        await engine.edit_input("keep", "edited durable input")
        await engine.retarget_input("keep", InboxTarget.NEXT_STEP)
        await engine.remove_input("remove")

        restored_provider = MockLLM(responses=[
            {"content": "session title"},
            {"content": "response to restored input"},
        ])
        resumed_services = await start_application(
            paths=paths,
            session_id="edited-durable-inbox",
            thread_id="agent",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=restored_provider,
        )
        resumed_engine = resumed_services.engine
        assert [
            (item.id, item.target, item.input.content)
            for item in resumed_engine.pending_inputs
        ] == [
            ("keep", InboxTarget.NEXT_STEP, "edited durable input"),
        ]

        events = [
            event
            async for event in resumed_engine.run_pending(
                request_id="restored-queue"
            )
        ]
        assert events[-1].kind == "turn_ended"
        assert resumed_engine.pending_input_count == 0
        assert [
            _record_text(project_message(message))
            for message in resumed_engine.messages
        ] == ["edited durable input", "response to restored input"]
        provider_user_text = [
            part.text
            for request in restored_provider.request_history
            for message in request.messages
            if isinstance(message, ProviderUser)
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        assert "edited durable input" in provider_user_text
        assert "discard this" not in provider_user_text
    finally:
        if resumed_services is not None:
            await resumed_services.stop()
        await services.stop()


@pytest.mark.asyncio
async def test_session_manager_resumes_durable_inbox_after_owner_process_crashes(
    temp_data_dir,
    temp_workspace,
):
    child_program = """
import asyncio
import os
import sys
from pathlib import Path
from XBotv2.agentloop.contracts import HumanInput, InboxItem
from XBotv2.application.app import start_application
from XBotv2.core.domain import InboxTarget
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM

async def main():
    app = await start_application(
        paths=RuntimePaths.from_data_dir(Path(sys.argv[1])),
        session_id='crashed-pending-inbox',
        thread_id='agent',
        workspace_root=Path(sys.argv[2]),
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    await app.engine.start_session()
    await app.engine.submit_input(
        InboxItem(
            id='durable-before-crash',
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content='resume this durable inbox item'),
        ),
        wake=False,
    )
    persistence = app.get('thread_persistence')
    assert persistence is not None
    assert [item.id for item in persistence.inbox.load()] == ['durable-before-crash']
    os.write(1, b'pending-input-durable\\n')

asyncio.run(main())
os._exit(0)
"""
    repository_root = Path(__file__).resolve().parents[3]
    python_path = os.pathsep.join(filter(None, (
        str(repository_root / "XBotv2"),
        os.environ.get("PYTHONPATH", ""),
    )))
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_program,
            str(temp_data_dir),
            str(temp_workspace),
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": python_path},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "pending-input-durable"

    class Events:
        async def emit(self, *_args, **_kwargs):
            return None

    paths = RuntimePaths.from_data_dir(temp_data_dir)
    provider = MockLLM(responses=[
        {"content": "restored session title"},
        {"content": "restored inbox response"},
    ])
    manager = SessionManager(
        paths,
        Events(),
        application_factory=create_agent_application,
        thread_persistence_factory=thread_persistence_factory,
        idle_timeout=None,
    )
    runtime = await manager.open_session(
        session_id="crashed-pending-inbox",
        thread_id="agent",
        provider_name=None,
        workspace_root=str(temp_workspace),
        mode="resume",
        no_plugins=False,
        llm_override=provider,
    )
    try:
        for _ in range(200):
            if (
                runtime.engine.pending_input_count == 0
                and len(provider.request_history) >= 2
                and any(
                    isinstance(message, AssistantMessage)
                    for message in runtime.engine.messages
                )
            ):
                break
            await asyncio.sleep(0.01)

        assert runtime.engine.pending_input_count == 0
        records = [
            record for message in runtime.engine.messages
            if (record := project_message(message)) is not None
        ]
        assert [record.kind for record in records] == [
            "human_input", "assistant",
        ]
        assert records[0].content == "resume this durable inbox item"
        assert records[1].content == "restored inbox response"
        request_text = [
            part.text
            for request in provider.request_history
            for message in request.messages
            if isinstance(message, ProviderUser)
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        assert any("resume this durable inbox item" in text for text in request_text)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_inflight_provider_stream_is_discarded_but_input_history_survives_crash(
    temp_data_dir,
    temp_workspace,
    tmp_path,
):
    marker = tmp_path / "provider-request-started"
    child_program = """
import asyncio
import sys
from pathlib import Path
from XBotv2.agentloop.contracts import HumanInput, InboxItem
from XBotv2.application.app import start_application
from XBotv2.core.domain import InboxTarget
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM
from XBotv2.session.records import project_message

class BlockAfterCaption(MockLLM):
    def __init__(self, marker):
        super().__init__(responses=[{'content': 'crash recovery title'}])
        self.marker = Path(marker)

    async def _astream_once(self, request):
        if self.call_count == 0:
            async for event in super()._astream_once(request):
                yield event
            return
        self._state.call_count += 1
        self._state.request_history.append(request)
        self.marker.write_text('started', encoding='utf-8')
        await asyncio.Event().wait()

async def main():
    provider = BlockAfterCaption(sys.argv[3])
    app = await start_application(
        paths=RuntimePaths.from_data_dir(Path(sys.argv[1])),
        session_id='crashed-active-turn',
        thread_id='agent',
        workspace_root=Path(sys.argv[2]),
        plugin_dirs=[],
        llm_override=provider,
    )
    async for _event in app.engine.run_turn(
        InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content='request interrupted by owner crash'),
        ),
        request_id='crashed-active-request',
    ):
        pass

asyncio.run(main())
"""
    repository_root = Path(__file__).resolve().parents[3]
    python_path = os.pathsep.join(filter(None, (
        str(repository_root / "XBotv2"),
        os.environ.get("PYTHONPATH", ""),
    )))
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_program,
            str(temp_data_dir),
            str(temp_workspace),
            str(marker),
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": python_path},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(1000):
            if marker.is_file() or process.poll() is not None:
                break
            await asyncio.sleep(0.01)
        if not marker.is_file():
            stdout, stderr = process.communicate(timeout=10)
            pytest.fail(
                "child never entered the in-flight provider request; "
                f"returncode={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        process.kill()
        _stdout, _stderr = process.communicate(timeout=10)
        assert process.returncode == -9
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    provider = MockLLM(responses=[{"content": "continued after crash"}])
    resumed = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="crashed-active-turn",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        recovered = [
            record for message in resumed.engine.messages
            if (record := project_message(message)) is not None
        ]
        assert [record.kind for record in recovered] == ["human_input"]
        assert recovered[0].content == "request interrupted by owner crash"
        assert provider.call_count == 0

        events = [event async for event in resumed.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="continue after interrupted turn"),
            ),
            request_id="after-crash-follow-up",
        )]
        assert events[-1].kind == "turn_ended"
        assert provider.call_count == 1
        user_text = [
            part.text
            for message in provider.request_history[-1].messages
            if isinstance(message, ProviderUser)
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        assert any("request interrupted by owner crash" in text for text in user_text)
        assert any("continue after interrupted turn" in text for text in user_text)
    finally:
        await resumed.stop()


@pytest.mark.asyncio
async def test_tool_side_effect_is_not_replayed_after_process_crash(
    temp_data_dir,
    temp_workspace,
    tmp_path,
):
    marker = tmp_path / "tool-effect-started"
    effect_log = tmp_path / "tool-effects.txt"
    child_program = """
import asyncio
import os
import sys
from pathlib import Path
from XBotv2.agentloop.contracts import HumanInput, InboxItem
from XBotv2.application.app import start_application
from XBotv2.core.domain import InboxTarget
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import Tool
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions import PermissionPolicy, PermissionRule

async def main():
    async def irreversible() -> str:
        with Path(sys.argv[4]).open('a', encoding='utf-8') as stream:
            stream.write('effect\\n')
            stream.flush()
            os.fsync(stream.fileno())
        Path(sys.argv[3]).write_text('started', encoding='utf-8')
        await asyncio.Event().wait()

    app = await start_application(
        paths=RuntimePaths.from_data_dir(Path(sys.argv[1])),
        session_id='crashed-tool-turn',
        thread_id='agent',
        workspace_root=Path(sys.argv[2]),
        plugin_dirs=[],
        llm_override=MockLLM(responses=[
            {'content': 'tool crash title'},
            {'tool_calls': [{
                'id': 'irreversible-call',
                'name': 'irreversible',
                'args': {},
            }]},
        ]),
    )
    app.engine.tools.register(Tool.from_function(irreversible), cleanup='caller')
    app.permissions.replace_policies((PermissionPolicy(rules=(
        PermissionRule(tool_pattern='.*', decision='allow'),
    )),))
    async for _event in app.engine.run_turn(
        InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content='perform one irreversible action'),
        ),
        request_id='crashed-tool-request',
    ):
        pass

asyncio.run(main())
"""
    repository_root = Path(__file__).resolve().parents[3]
    python_path = os.pathsep.join(filter(None, (
        str(repository_root / "XBotv2"),
        os.environ.get("PYTHONPATH", ""),
    )))
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_program,
            str(temp_data_dir),
            str(temp_workspace),
            str(marker),
            str(effect_log),
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": python_path},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(1000):
            if marker.is_file() or process.poll() is not None:
                break
            await asyncio.sleep(0.01)
        if not marker.is_file():
            stdout, stderr = process.communicate(timeout=10)
            pytest.fail(
                "child did not reach the irreversible tool side effect; "
                f"returncode={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        process.kill()
        _stdout, _stderr = process.communicate(timeout=10)
        assert process.returncode == -9
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


    assert effect_log.read_text(encoding="utf-8") == "effect\n"

    class Events:
        async def emit(self, *_args, **_kwargs):
            return None

    paths = RuntimePaths.from_data_dir(temp_data_dir)
    provider = MockLLM(responses=[{"content": "continue after recovered failure"}])
    manager = SessionManager(
        paths,
        Events(),
        application_factory=create_agent_application,
        thread_persistence_factory=thread_persistence_factory,
        idle_timeout=None,
    )
    runtime = await manager.open_session(
        session_id="crashed-tool-turn",
        thread_id="agent",
        provider_name=None,
        workspace_root=str(temp_workspace),
        mode="resume",
        no_plugins=False,
        llm_override=provider,
    )
    reopened_invocations = []

    async def irreversible() -> str:
        reopened_invocations.append(True)
        with effect_log.open("a", encoding="utf-8") as stream:
            stream.write("effect\n")
        return "effect repeated"

    runtime.engine.tools.register(Tool.from_function(irreversible), cleanup="caller")
    try:
        records = [
            record for message in runtime.engine.messages
            if (record := project_message(message)) is not None
        ]
        assert [record.kind for record in records] == [
            "human_input", "assistant", "tool",
        ]
        assert isinstance(records[-1].outcome, ToolFailed)
        assert records[-1].outcome.error.code == "session_restarted"

        events = [event async for event in runtime.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="continue after uncertain tool outcome"),
            ),
            request_id="after-crashed-tool-follow-up",
        )]
        assert events[-1].kind == "turn_ended"
        assert reopened_invocations == []
        assert effect_log.read_text(encoding="utf-8") == "effect\n"
        assert provider.call_count == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_runtime_close_propagates_engine_failure_after_releasing_resources(
    temp_data_dir,
    temp_workspace,
    monkeypatch,
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await start_application(
        paths=paths,
        session_id="close-failure",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    application = await mounted_application(services)
    runtime = SessionRuntime(paths, False, application, services.engine)
    subscription = runtime.event_stream.subscribe()
    application_closed = False

    async def fail_engine_close():
        raise RuntimeError("engine close failed")

    monkeypatch.setattr(services.engine, "close_session", fail_engine_close)
    original_application_close = type(application).close

    async def record_application_close(instance):
        nonlocal application_closed
        await original_application_close(instance)
        application_closed = True

    monkeypatch.setattr(
        type(application), "close", record_application_close,
    )

    with pytest.raises(RuntimeError, match="engine close failed"):
        await runtime.close("test_close_failure")

    assert application_closed
    assert application.loop_state.session.status == "closed"
    with pytest.raises(StopAsyncIteration):
        await subscription.__anext__()
    ownership = acquire_session(
        paths.session("close-failure").root,
        label="test/close-failure-released",
    )
    ownership.release()


@pytest.mark.asyncio
async def test_runtime_close_releases_resources_after_inbox_discard_failure(
    temp_data_dir,
    temp_workspace,
    monkeypatch,
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await start_application(
        paths=paths,
        session_id="discard-close-failure",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    application = await mounted_application(services)
    runtime = SessionRuntime(paths, False, application, services.engine)
    subscription = runtime.event_stream.subscribe()
    application_closed = False

    async def fail_discard():
        raise RuntimeError("inbox discard failed")

    monkeypatch.setattr(services.engine, "discard_inputs", fail_discard)
    original_application_close = type(application).close

    async def record_application_close(instance):
        nonlocal application_closed
        await original_application_close(instance)
        application_closed = True

    monkeypatch.setattr(type(application), "close", record_application_close)

    with pytest.raises(RuntimeError, match="inbox discard failed"):
        await runtime.close("test_discard_failure")

    assert application_closed
    assert application.loop_state.session.status == "closed"
    with pytest.raises(StopAsyncIteration):
        await subscription.__anext__()
    ownership = acquire_session(
        paths.session("discard-close-failure").root,
        label="test/discard-close-failure-released",
    )
    ownership.release()
