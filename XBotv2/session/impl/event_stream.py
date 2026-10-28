"""Bounded replay for one active Session runtime's shared events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pydantic_core import to_json
from XBotv2.core.api.domain import EventScope, SessionScope
from XBotv2.session.api.contracts import SessionEventCursorExpired, SessionEventFrame, SessionEventSubscription as SessionEventSubscriptionPort, SessionEvent
from XBotv2.agentloop.api.state import ThreadExecution
from XBotv2.session.api.replay import ReplayPoolPort


@dataclass(slots=True, eq=False)
class _SessionSubscriber:
    """A one-slot wakeup for a cursor over the central replay window."""

    wakeups: asyncio.Queue[None]
    closed: bool = False


class _SessionEventSubscription(AsyncIterator[SessionEventFrame]):
    def __init__(
        self,
        stream: "SessionEventStream",
        subscriber: _SessionSubscriber,
        cursor: int,
    ) -> None:
        self._stream = stream
        self._subscriber = subscriber
        self._cursor = cursor
        self._closed = False

    def __aiter__(self) -> "_SessionEventSubscription":
        return self

    async def __anext__(self) -> SessionEventFrame:
        try:
            while not self._closed and not self._subscriber.closed:
                frame = self._stream.frame_after(self._cursor)
                if frame is not None:
                    self._cursor = frame.sequence
                    return frame
                await self._subscriber.wakeups.get()
            raise StopAsyncIteration
        except BaseException:
            self.close()
            raise

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.detach(self._subscriber)


class SessionEventStream:
    def __init__(
        self,
        state: ThreadExecution,
        *,
        pool: ReplayPoolPort,
        capacity: int = 512,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if capacity < 1 or max_bytes < 1:
            raise ValueError("Session event capacity and byte budget must be positive")
        self._window = pool.window(capacity=capacity, max_bytes=max_bytes)
        self._subscribers: set[_SessionSubscriber] = set()
        self._state = state
        self._closed = False

    @property
    def sequence(self) -> int:
        return self._state.event_cursor

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def oldest_sequence(self) -> int:
        frame = self._window.oldest
        return frame.sequence if frame is not None else self.sequence + 1

    def publish(
        self,
        event: SessionEvent,
        *,
        scope: EventScope = SessionScope(),
    ) -> SessionEventFrame:
        if self._closed:
            raise RuntimeError("Session event stream is closed")
        frame = SessionEventFrame(self.sequence + 1, scope, event)
        size = len(to_json(frame))
        self._state.event_cursor = frame.sequence
        self._window.append(frame, size)
        return frame

    def subscribe(self, after: int | None = None) -> SessionEventSubscriptionPort:
        if self._closed:
            raise RuntimeError("Session event stream is closed")
        cursor = self.sequence if after is None else after
        if cursor < 0 or cursor > self.sequence:
            raise ValueError("Session event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if cursor < oldest - 1:
            raise SessionEventCursorExpired(cursor, oldest)
        subscriber = _SessionSubscriber(asyncio.Queue(maxsize=1))
        self._subscribers.add(subscriber)
        self._window.watch(subscriber.wakeups)
        return _SessionEventSubscription(self, subscriber, cursor)

    def frame_after(self, cursor: int) -> SessionEventFrame | None:
        if cursor < self.oldest_sequence - 1:
            raise SessionEventCursorExpired(cursor, self.oldest_sequence)
        next_sequence = cursor + 1
        if next_sequence > self.sequence:
            return None
        # ``deque`` is indexed relative to the oldest retained frame.  Using
        # the global sequence modulo capacity here is incorrect after the
        # first eviction because sequence zero is not the deque's origin.
        index = next_sequence - self.oldest_sequence
        frame = self._window.get(index)
        if frame is None:
            raise SessionEventCursorExpired(cursor, self.oldest_sequence)
        if frame.sequence != next_sequence:
            raise SessionEventCursorExpired(cursor, self.oldest_sequence)
        return frame

    def detach(self, subscriber: _SessionSubscriber) -> None:
        subscriber.closed = True
        self._subscribers.discard(subscriber)
        self._window.unwatch(subscriber.wakeups)
        if subscriber.wakeups.empty():
            subscriber.wakeups.put_nowait(None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscriber in tuple(self._subscribers):
            self.detach(subscriber)
        self._subscribers.clear()
        self._window.close()


