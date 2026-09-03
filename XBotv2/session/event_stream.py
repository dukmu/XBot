"""Bounded replay for one active Session runtime's shared events."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from XBotv2.core.tools import ClientEvent
from XBotv2.core.replay import (
    ReplaySubscriber,
    ReplaySubscription,
    fan_out,
)


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
    event: ClientEvent


class SessionEventSubscription(ReplaySubscription[SessionEventFrame]):
    def __init__(
        self,
        stream: "SessionEventStream",
        subscriber: ReplaySubscriber[SessionEventFrame],
        replay: tuple[SessionEventFrame, ...],
        cursor: int,
    ) -> None:
        super().__init__(
            stream=stream,
            subscriber=subscriber,
            replay=replay,
            cursor=cursor,
            cursor_error=SessionEventCursorExpired,
        )


class SessionEventStream:
    def __init__(self, *, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError("Session event capacity must be positive")
        self._capacity = capacity
        self._frames: deque[SessionEventFrame] = deque(maxlen=capacity)
        self._subscribers: set[ReplaySubscriber[SessionEventFrame]] = set()
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
        event: ClientEvent,
        *,
        request_id: str = "",
    ) -> SessionEventFrame:
        if self._closed:
            raise RuntimeError("Session event stream is closed")
        self._sequence += 1
        frame = SessionEventFrame(self._sequence, request_id, event)
        self._frames.append(frame)
        fan_out(self._subscribers, frame)
        return frame

    def subscribe(self, after: int | None = None) -> SessionEventSubscription:
        cursor = self._sequence if after is None else after
        if cursor < 0 or cursor > self._sequence:
            raise ValueError("Session event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if cursor < oldest - 1:
            raise SessionEventCursorExpired(cursor, oldest)
        replay = tuple(frame for frame in self._frames if frame.sequence > cursor)
        subscriber = ReplaySubscriber(asyncio.Queue(maxsize=self._capacity))
        self._subscribers.add(subscriber)
        return SessionEventSubscription(self, subscriber, replay, cursor)

    def detach(self, subscriber: ReplaySubscriber[SessionEventFrame]) -> None:
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
