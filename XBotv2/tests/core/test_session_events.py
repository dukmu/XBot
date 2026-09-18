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


@pytest.mark.asyncio
async def test_session_event_stream_uses_coalesced_wakeups_with_bounded_replay():
    """A wakeup carries no frame; the consumer pulls a contiguous window."""
    stream = SessionEventStream(capacity=3)
    subscription = stream.subscribe(0)

    for index in range(3):
        stream.publish(ClientEvent(type="usage", data={"index": index}))

    frames = [await subscription.__anext__() for _ in range(3)]

    assert [frame.sequence for frame in frames] == [1, 2, 3]

    for index in range(3, 6):
        stream.publish(ClientEvent(type="usage", data={"index": index}))
        assert (await subscription.__anext__()).sequence == index + 1
    await subscription.aclose()


@pytest.mark.asyncio
async def test_session_event_stream_handles_long_history_fast_and_slow_subscribers():
    """A fast subscriber keeps its cursor while a stalled one expires."""
    stream = SessionEventStream(capacity=8)
    for index in range(9992):
        stream.publish(ClientEvent(type="usage", data={"index": index}))

    fast = stream.subscribe(stream.sequence)
    slow = stream.subscribe(stream.sequence)

    fast_frames = []
    for index in range(9992, 10000):
        stream.publish(ClientEvent(type="usage", data={"index": index}))
        frame = await fast.__anext__()
        fast_frames.append(frame)

    assert [frame.sequence for frame in fast_frames] == list(range(9993, 10001))
    assert [frame.event.data["index"] for frame in fast_frames] == list(
        range(9992, 10000)
    )

    # Exactly capacity frames leave a cursor at oldest_sequence - 1 valid. One
    # more frame is required to demonstrate expiry of the stalled subscriber.
    stream.publish(ClientEvent(type="usage", data={"index": 10000}))
    with pytest.raises(SessionEventCursorExpired, match="expired"):
        await slow.__anext__()

    tail = stream.subscribe(stream.sequence - 8)
    tail_frames = [await tail.__anext__() for _ in range(8)]
    assert [frame.sequence for frame in tail_frames] == list(range(9994, 10002))
    assert [frame.event.data["index"] for frame in tail_frames] == list(
        range(9993, 10001)
    )

    with pytest.raises(SessionEventCursorExpired, match="expired"):
        stream.subscribe(0)

    await fast.aclose()
    await slow.aclose()
    await tail.aclose()


def test_session_event_stream_rejects_expired_and_future_cursors():
    stream = SessionEventStream(capacity=2)
    for index in range(3):
        stream.publish(ClientEvent(type="usage", data={"index": index}))

    with pytest.raises(SessionEventCursorExpired, match="expired"):
        stream.subscribe(0)
    with pytest.raises(ValueError, match="outside"):
        stream.subscribe(4)


def test_session_event_stream_ring_lookup_handles_middle_latest_and_eviction():
    stream = SessionEventStream(capacity=3)
    for index in range(4):
        stream.publish(ClientEvent(type="usage", data={"index": index}))

    assert stream.oldest_sequence == 2
    assert stream.frame_after(1).sequence == 2
    assert stream.frame_after(3).sequence == 4
    assert stream.frame_after(4) is None
    with pytest.raises(SessionEventCursorExpired):
        stream.frame_after(0)


@pytest.mark.asyncio
async def test_live_session_event_cursor_expiry_is_explicit():
    stream = SessionEventStream(capacity=2)
    subscription = stream.subscribe(0)
    for index in range(3):
        stream.publish(ClientEvent(type="usage", data={"index": index}))

    with pytest.raises(SessionEventCursorExpired, match="expired"):
        await subscription.__anext__()
    assert stream.subscriber_count == 0


@pytest.mark.asyncio
async def test_closing_unstarted_subscription_detaches_immediately():
    stream = SessionEventStream()
    subscription = stream.subscribe()

    await subscription.aclose()

    assert stream.subscriber_count == 0


@pytest.mark.asyncio
async def test_closing_waiting_subscription_wakes_only_that_subscriber():
    stream = SessionEventStream()
    waiting = stream.subscribe()
    active = stream.subscribe()
    waiting_task = asyncio.create_task(waiting.__anext__())
    await asyncio.sleep(0)

    await waiting.aclose()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting_task, timeout=1)
    assert stream.subscriber_count == 1

    first = stream.publish(ClientEvent(type="usage", data={"index": 1}))
    second = stream.publish(ClientEvent(type="usage", data={"index": 2}))
    assert await active.__anext__() == first
    assert await active.__anext__() == second
    await active.aclose()


@pytest.mark.asyncio
async def test_closing_session_event_stream_completes_subscribers():
    stream = SessionEventStream()
    subscription = stream.subscribe()

    stream.close()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription), timeout=1)
