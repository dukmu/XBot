"""Session runtime event replay and cursor semantics."""

import asyncio

import pytest

from XBotv2.core import ClientEvent
from XBotv2.session.event_stream import (
    SessionEventCursorExpired,
    SessionEventStream,
)


@pytest.mark.asyncio
async def test_session_event_stream_replays_then_follows_live_frames():
    stream = SessionEventStream(capacity=3)
    first = stream.publish(
        ClientEvent(type="message", data={"content": "one"}),
        request_id="request-1",
    )
    subscription = stream.subscribe(0)
    second = stream.publish(ClientEvent(type="usage", data={"total_tokens": 2}))

    replayed = await subscription.__anext__()
    live = await subscription.__anext__()

    assert replayed == first
    assert live == second
    assert replayed.request_id == "request-1"
    await subscription.aclose()


def test_session_event_stream_rejects_expired_and_future_cursors():
    stream = SessionEventStream(capacity=2)
    for index in range(3):
        stream.publish(ClientEvent(type="usage", data={"index": index}))

    with pytest.raises(SessionEventCursorExpired, match="expired"):
        stream.subscribe(0)
    with pytest.raises(ValueError, match="outside"):
        stream.subscribe(4)


@pytest.mark.asyncio
async def test_closing_session_event_stream_completes_subscribers():
    stream = SessionEventStream()
    subscription = stream.subscribe()

    stream.close()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription), timeout=1)
