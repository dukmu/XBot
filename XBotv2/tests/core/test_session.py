"""Core live-session lifecycle tests independent of HTTP."""

import asyncio
import json

import pytest

from api.paths import RuntimePaths
from core.session import SessionRuntime


class FakeEngine:
    plugin_loader = None
    job_registry = None
    input_window = False
    turn_count = 1
    continuation = False
    request_continuation = None
    take_pending_fold = None
    drain_inbox = None

    def __init__(self) -> None:
        self.client_event_sink = None
        self.closed = False
        self.close_count = 0

    def set_client_event_sink(self, sink):
        previous = self.client_event_sink
        self.client_event_sink = sink
        return previous

    async def run_turn(
        self,
        content,
        *,
        request_id="",
        images=None,
        artifacts=None,
    ):
        del content, request_id, images, artifacts
        yield {"type": "turn_started", "data": {"turn": 1}}
        yield {"type": "assistant_message", "data": {"content": "reply"}}
        yield {"type": "turn_finished", "data": {"turn": 1}}

    async def close_session(self):
        self.closed = True
        self.close_count += 1


def runtime(tmp_path) -> SessionRuntime:
    return SessionRuntime(
        session_id="session",
        thread_id="agent",
        provider_name="mock",
        paths=RuntimePaths.from_data_dir(tmp_path),
        workspace_root=str(tmp_path),
        no_plugins=True,
        engine=FakeEngine(),
    )


@pytest.mark.asyncio
async def test_idle_user_turn_runs_directly(tmp_path):
    session = runtime(tmp_path)

    events = [event async for event in session.stream_message("start", "request")]

    assert [
        event["data"]["content"]
        for event in events
        if event["type"] == "assistant_message"
    ] == ["reply"]
    assert not session.pending_fold
    await session.close()
    assert session.engine.closed is True
    assert session.engine.close_count == 1


@pytest.mark.asyncio
async def test_completion_stages_into_inbox_without_a_turn(tmp_path):
    session = runtime(tmp_path)
    events = session.attach_event_stream()

    await session._collect_completion({
        "type": "background_task",
        "kind": "background_task",
        "task_id": "t1",
        "status": "completed",
        "command": "printf x",
        "data": {},
    })

    assert len(session.inbox) == 1
    notice = await asyncio.wait_for(events.get(), timeout=1)
    assert notice["type"] == "completion_notice"
    assert session.turn_task is None, "completion must not start a turn"
    await session.close()


@pytest.mark.asyncio
async def test_busy_turn_holds_input_for_fold_delivery(tmp_path):
    session = runtime(tmp_path)
    await session.turn_lock.acquire()
    try:
        # A message submitted while the turn is busy is held in the pending
        # fold and fused at the next tool boundary.
        stream = session.stream_message("queued", "request")

        async def _first():
            return await anext(stream)

        first_task = asyncio.create_task(_first())
        await asyncio.sleep(0)
        assert len(session.pending_fold) == 1
        events = session.attach_event_stream()
        items = session._take_pending_fold()
        assert len(items) == 1
        assert session.fold_output is items[0].events
        msg = await asyncio.wait_for(events.get(), timeout=1)
        assert msg["type"] == "message"
        assert msg["data"]["role"] == "user"
        assert msg["data"]["content"] == "queued"
        assert msg["data"]["id"]
    finally:
        session.turn_lock.release()
    await session.close()
