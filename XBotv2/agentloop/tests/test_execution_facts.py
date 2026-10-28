import asyncio

import pytest

from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.agentloop.api.events import Events
from XBotv2.agentloop.api.outputs import ExecutionEnded, LoopTurnStarted
from XBotv2.agentloop.api.storage import ARCHIVE, ExecutionRecord
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.stream import ModelCompleted
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.tests.runtime import thread_scope
from XBotv2.main import open_runtime


async def wait_idle(loop):
    async with asyncio.timeout(10):
        while loop.busy:
            await asyncio.sleep(.005)


async def test_execution_events_and_cold_archive(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="s", llm_override=MockLLM([{"content": "answer"}]), extra_plugins=[{"id": "caption", "config": {"auto": False}}])
    loop = scope.require(AGENT_LOOP)
    archive = scope.root.require(ARCHIVE)
    events = scope.require(THREAD_EVENTS).subscribe()
    try:
        await loop.send_message("first", "one")
        await wait_idle(loop)
        result = await archive.latest_execution("s", "main")
        assert result.status == "completed" and result.input_ids == ("one",)
        assert result.final_message_id
        assert (await archive.message("s", "main", result.final_message_id)).kind == "assistant"
        async def fail(_):
            raise RuntimeError("before turn started")
        remove = scope.on(Events.ON_TURN_INPUT, fail)
        try:
            await loop.send_message("second", "two")
            await wait_idle(loop)
        finally:
            remove()
        failure = await archive.latest_execution("s", "main")
        assert failure.status == "failed" and failure.input_ids == ("two",)
        assert failure.final_message_id is None
        observed = []
        async with asyncio.timeout(5):
            while not any(isinstance(frame.event, ExecutionEnded) and frame.event.execution.turn_id == failure.turn_id for frame in observed):
                observed.append(await anext(events))
        failed_frames = [frame for frame in observed if getattr(frame.scope, "turn_id", None) == failure.turn_id]
        assert not any(isinstance(frame.event, LoopTurnStarted) for frame in failed_frames)
        assert failed_frames[-1].event.execution == failure
        assert not loop.busy
    finally:
        await events.aclose()
        await scope.root.close()
    root = await open_runtime(paths=paths)
    try:
        archive = root.require(ARCHIVE)
        assert await archive.latest_execution("s", "main") == failure
        assert (await archive.message("s", "main", result.final_message_id)).kind == "assistant"
    finally:
        await root.close()
    root = await open_runtime(paths=paths)
    try:
        from XBotv2.persistence.api.contracts import PERSISTENCE
        running = ExecutionRecord(turn_id="crashed", input_ids=("three",), status="running")
        claim = root.require(PERSISTENCE).acquire("s")
        try:
            archive = root.require(ARCHIVE)
            await archive._execution_start("s", "main", running)
        finally:
            claim.release()
    finally:
        await root.close()
    root = await open_runtime(paths=paths)
    try:
        assert await root.require(ARCHIVE).latest_execution("s", "main") == running
    finally:
        await root.close()


async def test_cancelled_execution_has_no_final_reply(tmp_path):
    started = asyncio.Event()
    class WaitingModel(MockLLM):
        async def _astream_once(self, request):
            started.set()
            await asyncio.Event().wait()
            yield ModelCompleted(response=self.to_response({"content": "unreachable"}))
    scope = await thread_scope(paths=RuntimePaths.from_data_dir(tmp_path / "data"), session_id="s", llm_override=WaitingModel([]))
    try:
        loop = scope.require(AGENT_LOOP)
        await loop.send_message("wait", "cancel-input")
        async with asyncio.timeout(10):
            await started.wait()
        assert await loop.interrupt()
        result = await scope.root.require(ARCHIVE).latest_execution("s", "main")
        assert result.status == "cancelled" and result.input_ids == ("cancel-input",)
        assert result.final_message_id is None and not loop.busy
    finally:
        await scope.root.close()


async def test_cold_input_status_and_execution_lookup(tmp_path):
    from XBotv2.agentloop.api.contracts import InboxItem, RuntimeInput
    from XBotv2.core.api.domain import InboxTarget

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(
        paths=paths, session_id="s", llm_override=MockLLM([{"content": "done"}]),
        extra_plugins=[{"id": "caption", "config": {"auto": False}}],
    )
    try:
        loop = scope.require(AGENT_LOOP)
        archive = scope.root.require(ARCHIVE)
        await loop.send_message("first", "completed-input")
        await wait_idle(loop)
        execution = await archive.execution_for_input("s", "main", "completed-input")
        assert execution.status == "completed" and execution.final_message_id
        task = InboxItem(id="task", target=InboxTarget.NEXT_TURN,
            input=RuntimeInput(source="parent", event="task", content="task"))
        other = InboxItem(id="other", target=InboxTarget.NEXT_TURN,
            input=RuntimeInput(source="parent", event="message", content="other"))
        await loop.submit_input(task, wake=False)
        await loop.inbox.remove("task")
        await loop.submit_input(other, wake=False)
        result = await archive.latest_input("s", "main", source="parent", event="task")
        assert result.input_id == "task" and result.status == "removed"
        assert result.target is InboxTarget.NEXT_TURN
        assert await archive.execution_for_input("s", "main", "task") is None
    finally:
        await scope.root.close()
    root = await open_runtime(paths=paths)
    try:
        archive = root.require(ARCHIVE)
        assert await archive.latest_input("s", "main", source="parent", event="task") == result
        assert (await archive.latest_input("s", "main", source="parent")).input_id == "other"
        assert await archive.execution_for_input("s", "main", "completed-input") == execution
        assert await archive.execution_for_input("s", "main", "task") is None
    finally:
        await root.close()


async def test_execution_identity_live_and_exact_cold_lookup(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class WaitingModel(MockLLM):
        async def _astream_once(self, request):
            started.set()
            await release.wait()
            yield ModelCompleted(response=self.to_response({"content": "done"}))

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(
        paths=paths, session_id="s", llm_override=WaitingModel([]),
        extra_plugins=[{"id": "caption", "config": {"auto": False}}],
    )
    try:
        loop = scope.require(AGENT_LOOP)
        archive = scope.root.require(ARCHIVE)
        assert loop.execution_id is None
        await loop.send_message("one", "input-one")
        async with asyncio.timeout(10):
            await started.wait()
        first_id = loop.execution_id
        assert first_id is not None
        first = await archive.execution_for_turn("s", "main", first_id)
        assert first.status == "running" and first.input_ids == ("input-one",)
        assert await archive.execution_for_turn("s", "main", "missing") is None
        release.set()
        await wait_idle(loop)
        assert loop.execution_id is None
        first = await archive.execution_for_turn("s", "main", first_id)
        assert first.status == "completed" and first.final_message_id
        await loop.send_message("two", "input-two")
        await wait_idle(loop)
        assert (await archive.latest_execution("s", "main")).turn_id != first_id
        assert await archive.execution_for_turn("s", "main", first_id) == first
    finally:
        release.set()
        await scope.root.close()
    root = await open_runtime(paths=paths)
    try:
        assert await root.require(ARCHIVE).execution_for_turn("s", "main", first_id) == first
    finally:
        await root.close()


async def test_duplicate_user_input_does_not_wake_a_passive_notice(tmp_path):
    from XBotv2.agentloop.api.contracts import InboxItem, RuntimeInput
    from XBotv2.core.api.domain import InboxTarget

    model = MockLLM([{"content": "done"}])
    scope = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), session_id="s",
        llm_override=model, extra_plugins=[{"id": "caption", "config": {"auto": False}}],
    )
    loop = scope.require(AGENT_LOOP)
    try:
        await loop.send_message("original", "original-id")
        await wait_idle(loop)
        await loop.submit_input(InboxItem(
            id="passive", target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(source="test", event="notice", content="passive reminder"),
        ), wake=False)
        await loop.send_message("original", "original-id")
        await asyncio.sleep(0)
        assert model.call_count == 1
        assert loop.wakeup_task is None
        assert [item.message_id for item in loop.pending_inputs()] == ["passive"]
    finally:
        await scope.root.close()
