"""Shared bounded replay subscription mechanics for event streams."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, MutableSet
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


class ReplayFrame(Protocol):
    @property
    def sequence(self) -> int: ...


FrameT = TypeVar("FrameT", bound=ReplayFrame)


@dataclass(slots=True, eq=False)
class ReplaySubscriber(Generic[FrameT]):
    queue: asyncio.Queue[FrameT | None]
    overflowed: bool = False


def fan_out(
    subscribers: MutableSet[ReplaySubscriber[FrameT]],
    frame: FrameT,
) -> None:
    for subscriber in tuple(subscribers):
        try:
            subscriber.queue.put_nowait(frame)
        except asyncio.QueueFull:
            subscriber.overflowed = True
            subscribers.discard(subscriber)


class ReplayStreamOwner(Protocol[FrameT]):
    @property
    def oldest_sequence(self) -> int: ...

    def detach(self, subscriber: ReplaySubscriber[FrameT]) -> None: ...


class ReplaySubscription(AsyncIterator[FrameT], Generic[FrameT]):
    """Consume replayed frames followed by live frames from one stream."""

    def __init__(
        self,
        *,
        stream: ReplayStreamOwner[FrameT],
        subscriber: ReplaySubscriber[FrameT],
        replay: tuple[FrameT, ...],
        cursor: int,
        cursor_error: type[LookupError],
    ) -> None:
        self._stream = stream
        self._subscriber = subscriber
        self._replay = deque(replay)
        self._cursor = cursor
        self._cursor_error = cursor_error
        self._closed = False

    def __aiter__(self) -> "ReplaySubscription[FrameT]":
        return self

    async def __anext__(self) -> FrameT:
        if self._closed:
            raise StopAsyncIteration
        if self._subscriber.overflowed:
            self._close()
            raise self._cursor_error(
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


__all__ = [
    "ReplayFrame",
    "ReplayStreamOwner",
    "ReplaySubscriber",
    "ReplaySubscription",
]
