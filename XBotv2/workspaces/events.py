"""Replayable process catalog changes owned by the Workspace plugin."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from XBotv2.session.contracts import (
    SessionResourceChanged,
    SessionResourceRemoved,
)
from XBotv2.workspaces.contracts import (
    ArchivedSessionsChanged,
    WorkspaceOrderChanged,
    WorkspaceResourceChanged,
    WorkspaceResourceRemoved,
)


WorkspaceCatalogChange = (
    SessionResourceChanged
    | SessionResourceRemoved
    | WorkspaceResourceChanged
    | WorkspaceResourceRemoved
    | WorkspaceOrderChanged
    | ArchivedSessionsChanged
)


class WorkspaceCursorExpired(LookupError):
    def __init__(self, cursor: int, oldest: int) -> None:
        super().__init__(
            f"Workspace event cursor {cursor} expired; "
            f"oldest available sequence is {oldest}"
        )
        self.cursor = cursor
        self.oldest = oldest


@dataclass(frozen=True, slots=True)
class WorkspaceEventFrame:
    sequence: int
    change: WorkspaceCatalogChange


@dataclass(slots=True, eq=False)
class _Subscriber:
    queue: asyncio.Queue[WorkspaceEventFrame]
    overflowed: bool = False


class WorkspaceEventSubscription(AsyncIterator[WorkspaceEventFrame]):
    def __init__(
        self,
        stream: "WorkspaceEventStream",
        subscriber: _Subscriber,
        replay: tuple[WorkspaceEventFrame, ...],
        cursor: int,
    ) -> None:
        self._stream = stream
        self._subscriber = subscriber
        self._replay = deque(replay)
        self._cursor = cursor
        self._closed = False

    def __aiter__(self) -> "WorkspaceEventSubscription":
        return self

    async def __anext__(self) -> WorkspaceEventFrame:
        if self._closed:
            raise StopAsyncIteration
        if self._subscriber.overflowed:
            self._closed = True
            self._stream.detach(self._subscriber)
            raise WorkspaceCursorExpired(
                self._cursor,
                self._stream.oldest_sequence,
            )
        frame = (
            self._replay.popleft()
            if self._replay
            else await self._subscriber.queue.get()
        )
        self._cursor = frame.sequence
        return frame

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.detach(self._subscriber)


class WorkspaceEventStream:
    """Bounded replay and live fan-out for committed catalog changes."""

    def __init__(self, *, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError("Workspace event capacity must be positive")
        self._capacity = capacity
        self._frames: deque[WorkspaceEventFrame] = deque(maxlen=capacity)
        self._subscribers: set[_Subscriber] = set()
        self._sequence = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def oldest_sequence(self) -> int:
        return self._frames[0].sequence if self._frames else self._sequence + 1

    def publish(self, change: WorkspaceCatalogChange) -> WorkspaceEventFrame:
        self._sequence += 1
        frame = WorkspaceEventFrame(self._sequence, change)
        self._frames.append(frame)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.queue.put_nowait(frame)
            except asyncio.QueueFull:
                subscriber.overflowed = True
                self._subscribers.discard(subscriber)
        return frame

    def subscribe(self, after: int) -> WorkspaceEventSubscription:
        if after < 0 or after > self._sequence:
            raise ValueError("Workspace event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if after < oldest - 1:
            raise WorkspaceCursorExpired(after, oldest)
        replay = tuple(frame for frame in self._frames if frame.sequence > after)
        subscriber = _Subscriber(asyncio.Queue(maxsize=self._capacity))
        self._subscribers.add(subscriber)
        return WorkspaceEventSubscription(self, subscriber, replay, after)

    def detach(self, subscriber: _Subscriber) -> None:
        self._subscribers.discard(subscriber)


__all__ = [
    "WorkspaceCatalogChange",
    "WorkspaceCursorExpired",
    "WorkspaceEventFrame",
    "WorkspaceEventStream",
    "WorkspaceEventSubscription",
]
