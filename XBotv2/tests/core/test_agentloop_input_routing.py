from XBotv2.tests.runtime import engine_of
from XBotv2.agentloop.api.contracts import AGENT_LOOP, INBOX
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.core.api.errors import OperationError
"""End-to-end checks for the loop-owned input path."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
import pytest
from XBotv2.agentloop.api.contracts import HumanInput, InboxItem, RuntimeInput
from XBotv2.core.api.domain import InboxTarget
from XBotv2.session.api.records import AssistantRecord, HumanInputRecord, RuntimeNoticeRecord, ToolRecord
from XBotv2.session.api.records import project_message
from XBotv2.core.api.tools import ToolFailed, ToolSucceeded, Tool
from XBotv2.agentloop.impl.execution import AgentLoop

from XBotv2.session.api.records import conversation_replay
from XBotv2.tests.runtime import thread_scope, engine_of
from XBotv2.core.api.messages import AssistantMessage
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.persistence.impl.session_lock import acquire_session
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.provider import ProviderUser
from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.session.impl.manager import SessionManager


async def _finished_turn(scope):
    events = scope.require(THREAD_EVENTS).subscribe(after=0)
    frames = []
    try:
        async with asyncio.timeout(5):
            async for frame in events:
                frames.append(frame.event)
                if frame.event.kind == "turn_ended":
                    return frames
    finally:
        await events.aclose()


async def _surface(scope):
    session = scope.require(LOOP_STATE).session
    return await scope.root.require(ARCHIVE).surface(session.session_id, session.thread_id)


def _record_text(record):
    if isinstance(record, (AssistantRecord, HumanInputRecord, RuntimeNoticeRecord)):
        return record.content
    if isinstance(record, ToolRecord) and isinstance(record.outcome, ToolSucceeded):
        return "".join(part.text for part in record.outcome.output.parts)
    return ""


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True])
@pytest.mark.parametrize("short_circuit", ["reject", "complete"])
async def test_short_circuited_batch_preserves_unprocessed_user_input(tmp_path, persistent, short_circuit):
    from XBotv2.agentloop.api.events import CompleteTurn, Events, RejectInput
    from XBotv2.agentloop.api.outputs import LoopError

    provider = MockLLM(responses=[{"content": "answered"}])
    application = await thread_scope(
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
        await engine_of(application).submit_input(InboxItem(
            id="accepted-notice", target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(source="job", event="completion", content="accepted notice"),
        ), wake=False)
        await engine_of(application).submit_input(notice, wake=False)
        events = [event async for event in engine_of(application).run_turn(user)]
        assert any(event.kind == "error" for event in events)
        assert provider.call_count == 0
        assert [message.id for message in engine_of(application).messages] == ["accepted-notice"]
        assert [item.id for item in engine_of(application).pending_inputs] == [user.id]
        if persistent:
            assert [item.id for item in application.require(INBOX).pending] == [user.id]
        retried = [event async for event in engine_of(application).run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert engine_of(application).pending_input_count == 0
        assert provider.call_count == 1
        assert [message.id for message in engine_of(application).messages if message.kind == "human_input"] == [user.id]
    finally:
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True])
@pytest.mark.parametrize("short_circuit", ["reject", "complete"])
@pytest.mark.parametrize("with_tool", [False, True])
async def test_step_short_circuit_is_published_and_leaves_suffix_pending(tmp_path, persistent, short_circuit, with_tool):
    from XBotv2.agentloop.api.events import CompleteTurn, Events, RejectInput
    from XBotv2.agentloop.api.outputs import LoopError

    first_response = (
        {"tool_calls": [{"id": "call-1", "name": "observe", "args": {}}]}
        if with_tool else {"content": "first"}
    )
    provider = MockLLM(responses=[first_response, {"content": "follow-up"}])
    application = await thread_scope(
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
            await engine_of(application).submit_input(item, wake=False)

    def handle(event):
        if event.input.id == "handled-input":
            if short_circuit == "reject":
                return RejectInput(error="rejected steering")
            return CompleteTurn(result=LoopError(code="handled", message="handled steering"))

    try:
        async def observe() -> str:
            """Return a deterministic tool result."""
            return "observed"

        engine_of(application).tools.register(Tool.from_function(observe), cleanup="caller")
        application.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern="observe", decision="allow"),
        )),))
        dispose = application.on(Events.MODEL_REQUEST_READY, enqueue)
        application.on(Events.ON_TURN_INPUT, handle)
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            id="initial", target=InboxTarget.NEXT_TURN, input=HumanInput(content="start"),
        ))]
        dispose()
        expected_code = "user_message_rejected" if short_circuit == "reject" else "handled"
        assert any(event.kind == "error" and event.code == expected_code for event in events)
        assert provider.call_count == 1
        assert sum(event.kind == "tool_completed" for event in events) == int(with_tool)
        assert sum(event.kind == "turn_ended" for event in events) == 1
        assert [item.id for item in engine_of(application).pending_inputs] == ["pending-suffix"]
        assert [message.id for message in engine_of(application).messages if message.kind == "human_input"] == [
            "initial", "accepted-prefix",
        ]
        if persistent:
            assert [item.id for item in application.require(INBOX).pending] == ["pending-suffix"]
        retried = [event async for event in engine_of(application).run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert provider.call_count == 2
        assert engine_of(application).pending_input_count == 0
    finally:
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_notice", [False, True])
@pytest.mark.parametrize("hook", ["accept", "canonicalize"])
async def test_input_hooks_cannot_break_consumption_identity(tmp_path, runtime_notice, hook):
    from XBotv2.agentloop.api.events import AcceptInput, Events, InputAccepted

    provider = MockLLM(responses=[{"content": "retried"}])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), workspace_root=tmp_path,
        no_plugins=True, llm_override=provider,
    )
    payload = RuntimeInput(source="job", event="done", content="result") if runtime_notice else HumanInput(content="question")
    item = InboxItem(id="original-input", target=InboxTarget.NEXT_TURN, input=payload)

    def replace_identity(event):
        if hook == "accept":
            return AcceptInput(event.input.model_copy(update={"id": "different-input"}))
        return InputAccepted(event.input, event.message.model_copy(update={"id": "different-input"}), event.source)

    try:
        dispose = application.on(
            Events.ON_TURN_INPUT if hook == "accept" else Events.INPUT_ACCEPTED,
            replace_identity,
        )
        events = [event async for event in engine_of(application).run_turn(item)]
        dispose()
        assert any(event.kind == "error" and "identity" in event.message for event in events)
        assert provider.call_count == 0
        assert engine_of(application).messages.snapshot() == ()
        assert await _surface(application) == ()
        assert application.require(INBOX).pending == [item]
        retried = [event async for event in engine_of(application).run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert engine_of(application).messages[0].id == item.id
        assert engine_of(application).pending_input_count == 0
    finally:
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["compact", "clear"])
async def test_resume_reconciles_inbox_against_trace_not_current_surface(tmp_path, operation, monkeypatch):
    from XBotv2.core.api.messages import CompactionSummaryMessage

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    async def start(mode="new"):
        return await thread_scope(mode=mode, paths=paths, workspace_root=tmp_path,
            session_id="reconcile-trace", no_plugins=True,
            llm_override=MockLLM(responses=[{"content": "done"}]))

    first = await start()
    inbox = first.require(INBOX)
    original_commit = inbox.commit
    async def crash_before_retirement(_ids):
        raise RuntimeError("simulated crash before inbox retirement")
    monkeypatch.setattr(inbox, "commit", crash_before_retirement)
    notice = InboxItem(id="committed-notice", target=InboxTarget.NEXT_STEP,
        input=RuntimeInput(source="job", event="completion", content="old notice"))
    user = InboxItem(id="committed-user", target=InboxTarget.NEXT_TURN, input=HumanInput(content="old input"))
    pending = InboxItem(id="unprocessed", target=InboxTarget.NEXT_TURN, input=HumanInput(content="new input"))
    try:
        await first.require(AGENT_LOOP).submit_input(notice, wake=False)
        async for _ in engine_of(first).run_turn(user):
            pass
        monkeypatch.setattr(inbox, "commit", original_commit)
        await first.require(AGENT_LOOP).submit_input(pending, wake=False)
        history = first.require(LOOP_STATE).history
        assert {notice.id, user.id}.intersection(m.id for m in history)
        replacement = (CompactionSummaryMessage(id="summary", summary="prior context"),) if operation == "compact" else ()
        await history.replace_range(0, len(history), replacement, operation=operation,
                                    preserve_transcript=operation == "compact")
    finally:
        await first.root.close()
    resumed = await start("resume")
    try:
        events = await _finished_turn(resumed)
        from XBotv2.session.api.events import InputClaimedEvent
        claimed = [identity for event in events if isinstance(event, InputClaimedEvent) for identity in event.message_ids]
        assert claimed == [pending.id]
        assert not any(event.kind == "error" for event in events)
        assert engine_of(resumed).pending_input_count == 0
    finally:
        await resumed.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_live_human_input_is_projected_from_accepted_history(tmp_path, rejected):
    from XBotv2.agentloop.api.events import Events, RejectInput
    from XBotv2.session.api.events import InputConsumedEvent, MessagePublishedEvent

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    context = await thread_scope(
        paths=paths, workspace_root=tmp_path,
        llm_override=MockLLM(responses=[{"content": "reply"}]),
        extra_plugins=[
            {"id": "caption", "disabled": True},
            {"id": "content_cache", "config": {
                "threshold_chars": 100, "preview_chars": 20, "tail_chars": 5,
            }},
        ],
    )
    application = context
    runtime = application.require(AGENT_LOOP)
    events = application.require(THREAD_EVENTS).subscribe()
    if rejected:
        context.on(Events.ON_TURN_INPUT, lambda _event: RejectInput(error="rejected"))
    try:
        original = "long user input " * 100
        async for _event in engine_of(context).run_turn(InboxItem(
            id="request-identity", target=InboxTarget.NEXT_TURN,
            input=HumanInput(content=original),
        )):
            pass
        frames = [
            await asyncio.wait_for(anext(events), timeout=1)
            for _ in range(application.require(THREAD_EVENTS).sequence)
        ]
        published = [
            frame.event.record.root for frame in frames
            if isinstance(frame.event, MessagePublishedEvent)
        ]
        history = [
            project_message(message) for message in await _surface(context)
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
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True])
@pytest.mark.parametrize("cancelled", [True, False])
async def test_failed_step_input_is_released_for_retry(tmp_path, persistent, cancelled):
    from XBotv2.agentloop.api.events import Events
    from XBotv2.core.api.messages import HumanInputMessage

    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        workspace_root=tmp_path, no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "first"}, {"content": "retried"}]),
        extra_plugins=[{"id": "persistence", "disabled": not persistent}],
    )
    steer = InboxItem(
        id="steering", target=InboxTarget.NEXT_STEP, input=HumanInput(content="correction"),
    )

    async def enqueue_while_running(_event):
        await engine_of(application).submit_input(steer, wake=False)

    accepting = asyncio.Event()

    async def fail_steering(event):
        if event.input.id == steer.id:
            accepting.set()
            if cancelled:
                await asyncio.Event().wait()
            raise RuntimeError("steering handler failed")

    async def run():
        return [event async for event in engine_of(application).run_turn(InboxItem(
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
        assert [item.id for item in engine_of(application).inbox.pending] == [steer.id]
        retried = [event async for event in engine_of(application).run_pending()]
        assert any(event.kind == "assistant_completed" for event in retried)
        assert sum(
            isinstance(message, HumanInputMessage) and message.id == steer.id
            for message in engine_of(application).messages
        ) == 1
        if persistent:
            assert application.require(INBOX).pending == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True])
async def test_partial_input_batch_does_not_replay_committed_notice(tmp_path, persistent):
    from XBotv2.agentloop.api.events import Events
    from XBotv2.core.api.messages import RuntimeNoticeMessage
    from XBotv2.session.api.events import MessagePublishedEvent

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    application = await thread_scope(
        paths=paths,
        workspace_root=tmp_path, no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "continued"}]),
        extra_plugins=[{"id": "persistence", "disabled": not persistent}],
    )
    runtime = application.require(AGENT_LOOP)
    stream = application.require(THREAD_EVENTS).subscribe()
    notice = InboxItem(
        id="completed-job", target=InboxTarget.NEXT_STEP,
        input=RuntimeInput(source="job", event="completion", content="job finished"),
    )
    user = InboxItem(id="followup", target=InboxTarget.NEXT_TURN, input=HumanInput(content="continue"))

    def fail_human_input(event):
        if event.input.id == user.id:
            raise RuntimeError("input handler failed")

    try:
        await engine_of(application).submit_input(notice, wake=False)
        dispose = application.on(Events.ON_TURN_INPUT, fail_human_input)
        failed = [event async for event in engine_of(application).run_turn(user)]
        assert any(event.kind == "error" for event in failed)
        dispose()
        assert [item.id for item in engine_of(application).inbox.pending] == [user.id]
        frames = [
            await asyncio.wait_for(anext(stream), timeout=1)
            for _ in range(application.require(THREAD_EVENTS).sequence)
        ]
        assert [
            frame.event.record.root for frame in frames
            if isinstance(frame.event, MessagePublishedEvent)
        ] == [project_message(engine_of(application).messages[0])]
        retried = [event async for event in engine_of(application).run_pending()]
        assert not any(event.kind == "error" for event in retried)
        messages = engine_of(application).messages
        assert sum(isinstance(message, RuntimeNoticeMessage) for message in messages) == 1
        assert engine_of(application).pending_input_count == 0
        if persistent:
            stored = await _surface(application)
            assert sum(isinstance(message, RuntimeNoticeMessage) for message in stored) == 1
            assert application.require(INBOX).pending == []
    finally:
        await stream.aclose()
        await application.root.close()


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
    services = await thread_scope(
        paths=paths,
        session_id="inbox-routing",
        thread_id="main",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        engine = engine_of(services)
        services.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        engine.tools.register(Tool.from_function(blocker), cleanup="caller")
        application = services
        runtime = application.require(AGENT_LOOP)

        async def collect(content: str, request_id: str):
            events = application.require(THREAD_EVENTS).subscribe()
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

    finally:
        await services.root.close()

@pytest.mark.asyncio
async def test_injected_notification_is_durable_and_does_not_wake(
    temp_data_dir,
    temp_workspace,
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await thread_scope(
        paths=paths,
        session_id="durable-inbox",
        thread_id="main",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    engine = engine_of(services)
    application = services
    runtime = application.require(AGENT_LOOP)
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
    assert not runtime.busy

    await services.root.close()
    resumed_services = await thread_scope(
        mode="resume", paths=paths, session_id="durable-inbox", thread_id="main",
        workspace_root=temp_workspace, llm_override=MockLLM([{"content": "observed"}]),
    )
    try:
        events = await _finished_turn(resumed_services)
        messages = resumed_services.require(LOOP_STATE).history.snapshot()
        assert [_record_text(project_message(message)) for message in messages] == ["job finished", "observed"]
        replayed = conversation_replay(messages)
        assert replayed[0].kind == "runtime_notice"
        assert replayed[0].source == "job"
        assert replayed[0].event == "completion"
        assert sum(event.kind == "turn_ended" for event in events) == 1
    finally:
        await resumed_services.root.close()



@pytest.mark.asyncio
async def test_edited_pending_input_is_hydrated_and_consumed_after_reopen(
    temp_data_dir,
    temp_workspace,
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await thread_scope(
        paths=paths,
        session_id="edited-durable-inbox",
        thread_id="main",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(),
    )
    resumed_services = None
    try:
        engine = engine_of(services)
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
        await services.root.close()
        resumed_services = await thread_scope(
            mode="resume",
            paths=paths,
            session_id="edited-durable-inbox",
            thread_id="main",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=restored_provider,
        )
        resumed_engine = engine_of(resumed_services)
        events = await _finished_turn(resumed_services)
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
            await resumed_services.root.close()
        await services.root.close()




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
from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
from XBotv2.tests.runtime import thread_scope, engine_of
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM

from XBotv2.session.api.records import project_message

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
    app = await thread_scope(
        paths=RuntimePaths.from_data_dir(Path(sys.argv[1])),
        session_id='crashed-active-turn',
        thread_id='main',
        workspace_root=Path(sys.argv[2]),
        plugin_dirs=[],
        llm_override=provider,
    )
    async for _event in engine_of(app).run_turn(
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
    resumed = await thread_scope(
        mode="resume",
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="crashed-active-turn",
        thread_id="main",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        recovered = [
            record for message in engine_of(resumed).messages
            if (record := project_message(message)) is not None
        ]
        assert [record.kind for record in recovered] == ["human_input"]
        assert recovered[0].content == "request interrupted by owner crash"
        assert provider.call_count == 0

        events = [event async for event in engine_of(resumed).run_turn(
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
        await resumed.root.close()




@pytest.mark.asyncio
async def test_scope_close_retains_resources_after_engine_cleanup_failure(tmp_path, monkeypatch):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="close-failure", workspace_root=tmp_path, llm_override=MockLLM())
    engine = engine_of(scope)
    original = engine.close_session
    async def fail():
        raise RuntimeError("engine close failed")
    monkeypatch.setattr(engine, "close_session", fail)
    try:
        with pytest.raises(ExceptionGroup) as failure:
            await scope.root.close()
        assert failure.value.subgroup(lambda error: isinstance(error, RuntimeError) and str(error) == "engine close failed") is not None
        assert scope.require(AGENT_LOOP) is not None
        with pytest.raises(OperationError, match="already owned"):
            acquire_session(paths.session("close-failure").root, label="competitor")
    finally:
        monkeypatch.setattr(engine, "close_session", original)
        await scope.root.close()
    claim = acquire_session(paths.session("close-failure").root, label="after-close")
    claim.release()


@pytest.mark.asyncio
async def test_scope_close_retries_failed_pre_stop_without_discarding_inbox(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="inbox-close-failure", workspace_root=tmp_path, llm_override=MockLLM())
    item = InboxItem(id="accepted", target=InboxTarget.NEXT_STEP, input=RuntimeInput(source="test", event="notification", content="preserved"))
    await scope.require(AGENT_LOOP).submit_input(item, wake=False)
    fault = True
    async def fail():
        if fault:
            raise RuntimeError("pre-stop failed")
    scope.pre_stop(fail)
    try:
        with pytest.raises(ExceptionGroup):
            await scope.root.close()
        assert [pending.id for pending in scope.require(INBOX).pending] == ["accepted"]
        with pytest.raises(OperationError, match="already owned"):
            acquire_session(paths.session("inbox-close-failure").root, label="competitor")
    finally:
        fault = False
        await scope.root.close()
    resumed = await thread_scope(paths=paths, session_id="inbox-close-failure", mode="resume", workspace_root=tmp_path, llm_override=MockLLM(responses=[{"content": "done"}]))
    try:
        async with asyncio.timeout(5):
            while True:
                messages = await _surface(resumed)
                if any(message.id == "accepted" for message in messages) and not resumed.require(AGENT_LOOP).busy:
                    break
                await asyncio.sleep(0.01)
        assert sum(message.id == "accepted" for message in messages) == 1
    finally:
        await resumed.root.close()
    reopened = await thread_scope(paths=paths, session_id="inbox-close-failure", mode="resume", workspace_root=tmp_path, llm_override=MockLLM())
    try:
        assert sum(message.id == "accepted" for message in await _surface(reopened)) == 1
        assert all(item.id != "accepted" for item in reopened.require(INBOX).pending)
    finally:
        await reopened.root.close()
