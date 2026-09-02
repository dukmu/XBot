"""Bounded replay for one active Session runtime's shared events."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from XBotv2.session.types import SessionStreamEvent


class SessionEventCursorExpired(LookupError):
    def __init__(self, cursor: int, oldest: int) -> None:
        super().__init__(
            f"Session event cursor {cursor} expired; "
            f"oldest available sequence is {oldest}"
        )
        self.cursor = cursor
        self.oldest = oldest


@dataclass(frozen=True, slots=True)
class SessionEventFrame:
    sequence: int
    request_id: str
    event: SessionStreamEvent


@dataclass(slots=True, eq=False)
class _Subscriber:
    queue: asyncio.Queue[SessionEventFrame | None]
    overflowed: bool = False


class SessionEventSubscription(AsyncIterator[SessionEventFrame]):
    def __init__(
        self,
        stream: "SessionEventStream",
        subscriber: _Subscriber,
        replay: tuple[SessionEventFrame, ...],
        cursor: int,
    ) -> None:
        self._stream = stream
        self._subscriber = subscriber
        self._replay = deque(replay)
        self._cursor = cursor
        self._closed = False

    def __aiter__(self) -> "SessionEventSubscription":
        return self

    async def __anext__(self) -> SessionEventFrame:
        if self._closed:
            raise StopAsyncIteration
        if self._subscriber.overflowed:
            self._close()
            raise SessionEventCursorExpired(
                self._cursor,
                self._stream.oldest_sequence,
            )
        frame = (
            self._replay.popleft()
            if self._replay
            else await self._subscriber.queue.get()
        )
        if frame is None:
            self._close()
            raise StopAsyncIteration
        self._cursor = frame.sequence
        return frame

    async def aclose(self) -> None:
        self._close()

    def close(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.detach(self._subscriber)


class SessionEventStream:
    def __init__(self, *, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError("Session event capacity must be positive")
        self._capacity = capacity
        self._frames: deque[SessionEventFrame] = deque(maxlen=capacity)
        self._subscribers: set[_Subscriber] = set()
        self._sequence = 0
        self._closed = False

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def oldest_sequence(self) -> int:
        return self._frames[0].sequence if self._frames else self._sequence + 1

    def publish(
        self,
        event: SessionStreamEvent | Mapping[str, object],
        *,
        request_id: str = "",
    ) -> SessionEventFrame:
        if self._closed:
            raise RuntimeError("Session event stream is closed")
        value = (
            event
            if isinstance(event, SessionStreamEvent)
            else SessionStreamEvent.model_validate(event)
        )
        self._sequence += 1
        frame = SessionEventFrame(self._sequence, request_id, value)
        self._frames.append(frame)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.queue.put_nowait(frame)
            except asyncio.QueueFull:
                subscriber.overflowed = True
                self._subscribers.discard(subscriber)
        return frame

    def subscribe(self, after: int | None = None) -> SessionEventSubscription:
        cursor = self._sequence if after is None else after
        if cursor < 0 or cursor > self._sequence:
            raise ValueError("Session event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if cursor < oldest - 1:
            raise SessionEventCursorExpired(cursor, oldest)
        replay = tuple(frame for frame in self._frames if frame.sequence > cursor)
        subscriber = _Subscriber(asyncio.Queue(maxsize=self._capacity))
        self._subscribers.add(subscriber)
        return SessionEventSubscription(self, subscriber, replay, cursor)

    def detach(self, subscriber: _Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscriber in tuple(self._subscribers):
            while subscriber.queue.full():
                subscriber.queue.get_nowait()
            subscriber.queue.put_nowait(None)
        self._subscribers.clear()


__all__ = [
    "SessionEventCursorExpired",
    "SessionEventFrame",
    "SessionEventStream",
    "SessionEventSubscription",
]
