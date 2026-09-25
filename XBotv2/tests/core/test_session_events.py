"""Session replay retains typed events in a bounded, shared sequence."""

from dataclasses import dataclass

import pytest

from XBotv2.core.domain import (
    AgentExecutionLimits,
    GenerationSettings,
    ModelRoute,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    StandardGenerationMode,
)
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from XBotv2.session.contracts import SessionKey, SessionRuntimeState
from XBotv2.session.event_stream import SessionEventCursorExpired, SessionEventStream


@dataclass(frozen=True)
class _Event:
    kind: str
    index: int


async def _stream(capacity=512):
    selection = ResolvedRuntimeSelection(
        agent_name="default",
        prompt="",
        limits=AgentExecutionLimits(),
        enabled_tools=(),
        model=ResolvedModelSelection(
            route=ModelRoute(provider="mock", model="test"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128,
            ),
            context_window=4096,
        ),
    )
    metadata_state = ThreadMetadataState(
        __import__("xcore").Context(), session_id="s", thread_id="t",
    )
    await metadata_state.initialize(ThreadMetadata(runtime_selection=selection))
    state = SessionRuntimeState(SessionKey("s", "t"), metadata_state)
    return SessionEventStream(state, capacity=capacity)


@pytest.mark.asyncio
async def test_session_event_stream_replays_then_follows_live_frames():
    stream = await _stream(capacity=3)
    first = stream.publish(_Event(kind="message", index=1))
    subscription = stream.subscribe(0)
    second = stream.publish(_Event(kind="usage", index=2))

    assert await subscription.__anext__() == first
    assert await subscription.__anext__() == second
    assert first.sequence == 1 and first.event.index == 1
    await subscription.aclose()


@pytest.mark.asyncio
async def test_session_event_stream_uses_coalesced_wakeups_with_bounded_replay():
    stream = await _stream(capacity=3)
    subscription = stream.subscribe(0)

    for index in range(3):
        stream.publish(_Event(kind="usage", index=index))
    frames = [await subscription.__anext__() for _ in range(3)]
    assert [frame.sequence for frame in frames] == [1, 2, 3]

    for index in range(3, 6):
        stream.publish(_Event(kind="usage", index=index))
        assert (await subscription.__anext__()).sequence == index + 1
    await subscription.aclose()


@pytest.mark.asyncio
async def test_session_event_stream_handles_long_history_and_slow_subscribers():
    stream = await _stream(capacity=8)
    for index in range(9992):
        stream.publish(_Event(kind="usage", index=index))

    fast = stream.subscribe(stream.sequence)
    slow = stream.subscribe(stream.sequence)
    fast_frames = []
    for index in range(9992, 10000):
        stream.publish(_Event(kind="usage", index=index))
        fast_frames.append(await fast.__anext__())

    assert [frame.sequence for frame in fast_frames] == list(range(9993, 10001))
    assert [frame.event.index for frame in fast_frames] == list(range(9992, 10000))

    stream.publish(_Event(kind="usage", index=10000))
    with pytest.raises(SessionEventCursorExpired, match="expired"):
        await slow.__anext__()

    tail = stream.subscribe(stream.sequence - 8)
    tail_frames = [await tail.__anext__() for _ in range(8)]
    assert [frame.sequence for frame in tail_frames] == list(range(9994, 10002))
    assert [frame.event.index for frame in tail_frames] == list(range(9993, 10001))

    with pytest.raises(SessionEventCursorExpired, match="expired"):
        stream.subscribe(0)

    await fast.aclose()
    await slow.aclose()
    await tail.aclose()


@pytest.mark.asyncio
async def test_session_event_stream_rejects_expired_and_future_cursors():
    stream = await _stream(capacity=2)
    for index in range(3):
        stream.publish(_Event(kind="usage", index=index))

    with pytest.raises(SessionEventCursorExpired, match="expired"):
        stream.subscribe(0)
    with pytest.raises(ValueError, match="outside"):
        stream.subscribe(4)
