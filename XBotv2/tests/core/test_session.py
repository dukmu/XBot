"""Core live-session lifecycle tests independent of HTTP."""

import asyncio
import json

import pytest

from xbotv2.api.paths import RuntimePaths
from xbotv2.core.session import SessionRuntime


class FakeEngine:
    def __init__(self) -> None:
        self.enqueue_mailbox = None
        self.client_event_sink = None
        self.closed = False
        self.close_count = 0

    def set_client_event_sink(self, sink):
        previous = self.client_event_sink
        self.client_event_sink = sink
        return previous

    async def run_mailbox_hook(self, stage, message, error=None):
        del stage, message, error

    @staticmethod
    def mailbox_content(item):
        return str(item.message)

    async def run_turn(self, content, *, request_id="", mailbox_message=None):
        del content, request_id
        yield {"type": "turn_started", "data": {"turn": 1}}
        if mailbox_message is None or mailbox_message.kind == "user_message":
            await self.enqueue_mailbox({"source": "test", "event": "continue"})
            reply = "first"
        else:
            reply = "continued"
        yield {"type": "assistant_message", "data": {"content": reply}}
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
async def test_idle_user_turn_bypasses_mailbox_and_general_uses_session_events(
    tmp_path,
):
    session = runtime(tmp_path)

    events = [event async for event in session.stream_message("start", "request")]

    assert [
        event["data"]["content"]
        for event in events
        if event["type"] == "assistant_message"
    ] == ["first"]
    assert session.mailbox.size == 1
    mailbox_log = session.paths.session("session").thread("agent").mailbox_log
    records = [
        json.loads(line)
        for line in mailbox_log.read_text(encoding="utf-8").splitlines()
    ]
    assert {
        record["item"]["kind"]
        for record in records
        if record["event"] == "enqueued"
    } == {"general"}

    session_events = session.attach_event_stream()
    continued = []
    while True:
        event = await asyncio.wait_for(session_events.get(), timeout=1)
        assert event is not None
        continued.append(event)
        if event["type"] == "turn_finished":
            break
    assert [
        event["data"]["content"]
        for event in continued
        if event["type"] == "assistant_message"
    ] == ["continued"]
    await session.close()
    assert session.engine.closed is True
    assert session.engine.close_count == 1


@pytest.mark.asyncio
async def test_session_event_stream_stays_open_between_general_turns(tmp_path):
    session = runtime(tmp_path)
    events = session.attach_event_stream()

    async def collect_turn():
        received = []
        while True:
            event = await asyncio.wait_for(events.get(), timeout=1)
            assert event is not None
            received.append(event)
            if event["type"] == "turn_finished":
                return received

    await session.enqueue_general("first")
    first = await collect_turn()
    await asyncio.sleep(0)
    await session.enqueue_general("second")
    second = await collect_turn()

    assert [
        event["data"]["content"]
        for event in [*first, *second]
        if event["type"] == "assistant_message"
    ] == ["continued", "continued"]
    await session.close()


@pytest.mark.asyncio
async def test_closing_queued_request_discards_only_that_message(tmp_path):
    session = runtime(tmp_path)
    session.turn_lock = asyncio.Lock()
    await session.turn_lock.acquire()
    stream = session.stream_message("queued", "request")

    queued = await anext(stream)
    await stream.aclose()
    session.turn_lock.release()

    assert queued["type"] == "message_queued"
    assert session.mailbox.size == 0
    assert session.engine.closed is False
    await session.close()
