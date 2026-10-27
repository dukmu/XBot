"""Bounded workspace event replay and fan-out."""

import asyncio
from collections import deque
from pydantic_core import to_json

from XBotv2.workspaces.api.events import WorkspaceCatalogChange, WorkspaceCursorExpired, WorkspaceEventFrame
from XBotv2.workspaces.impl.replay import ReplaySubscriber, ReplaySubscription


class WorkspaceEventStream:
    """Bounded replay and live fan-out for committed catalog changes."""

    def __init__(self, *, capacity: int = 512, max_bytes: int = 8 * 1024 * 1024) -> None:
        if capacity < 1 or max_bytes < 1:
            raise ValueError("Workspace event capacity and byte budget must be positive")
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._bytes = 0
        self._frames: deque[tuple[WorkspaceEventFrame, int]] = deque()
        self._subscribers: set[ReplaySubscriber] = set()
        self._sequence = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def oldest_sequence(self) -> int:
        return self._frames[0][0].sequence if self._frames else self._sequence + 1

    def publish(self, change: WorkspaceCatalogChange) -> WorkspaceEventFrame:
        frame = WorkspaceEventFrame(self._sequence + 1, change)
        size = len(to_json(frame))
        self._sequence = frame.sequence
        while self._frames and (len(self._frames) >= self._capacity or self._bytes + size > self._max_bytes):
            _, removed_size = self._frames.popleft()
            self._bytes -= removed_size
        if size <= self._max_bytes:
            self._frames.append((frame, size))
            self._bytes += size
        for subscriber in tuple(self._subscribers):
            if not subscriber.closed and subscriber.wakeups.empty():
                subscriber.wakeups.put_nowait(None)
        return frame

    def subscribe(self, after: int) -> ReplaySubscription:
        if after < 0 or after > self._sequence:
            raise ValueError("Workspace event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if after < oldest - 1:
            raise WorkspaceCursorExpired(after, oldest)
        subscriber = ReplaySubscriber(asyncio.Queue(maxsize=1))
        self._subscribers.add(subscriber)
        return ReplaySubscription(self, subscriber, after)

    def frame_after(self, cursor: int) -> WorkspaceEventFrame | None:
        oldest = self.oldest_sequence
        if cursor < oldest - 1:
            raise WorkspaceCursorExpired(cursor, oldest)
        index = cursor + 1 - oldest
        if index >= len(self._frames):
            return None
        frame = self._frames[index][0]
        if frame.sequence != cursor + 1:
            raise WorkspaceCursorExpired(cursor, oldest)
        return frame

    def detach(self, subscriber: ReplaySubscriber) -> None:
        subscriber.closed = True
        self._subscribers.discard(subscriber)
        if subscriber.wakeups.empty():
            subscriber.wakeups.put_nowait(None)
