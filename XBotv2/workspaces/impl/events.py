"""Bounded workspace event replay and fan-out."""

import asyncio
from pydantic_core import to_json

from XBotv2.workspaces.api.events import WorkspaceCatalogChange, WorkspaceCursorExpired, WorkspaceEventFrame
from XBotv2.workspaces.impl.replay import ReplaySubscriber, ReplaySubscription
from XBotv2.session.api.replay import ReplayPoolPort


class WorkspaceEventStream:
    """Bounded replay and live fan-out for committed catalog changes."""

    def __init__(self, *, pool: ReplayPoolPort, capacity: int = 512, max_bytes: int = 8 * 1024 * 1024) -> None:
        if capacity < 1 or max_bytes < 1:
            raise ValueError("Workspace event capacity and byte budget must be positive")
        self._window = pool.window(capacity=capacity, max_bytes=max_bytes)
        self._subscribers: set[ReplaySubscriber] = set()
        self._sequence = 0
        self._closed = False

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def oldest_sequence(self) -> int:
        frame = self._window.oldest
        return frame.sequence if frame is not None else self._sequence + 1

    def publish(self, change: WorkspaceCatalogChange) -> WorkspaceEventFrame:
        if self._closed:
            raise RuntimeError("Workspace event stream is closed")
        frame = WorkspaceEventFrame(self._sequence + 1, change)
        size = len(to_json(frame))
        self._sequence = frame.sequence
        self._window.append(frame, size)
        return frame

    def subscribe(self, after: int) -> ReplaySubscription:
        if self._closed:
            raise RuntimeError("Workspace event stream is closed")
        if after < 0 or after > self._sequence:
            raise ValueError("Workspace event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if after < oldest - 1:
            raise WorkspaceCursorExpired(after, oldest)
        subscriber = ReplaySubscriber(asyncio.Queue(maxsize=1))
        self._subscribers.add(subscriber)
        self._window.watch(subscriber.wakeups)
        return ReplaySubscription(self, subscriber, after)

    def frame_after(self, cursor: int) -> WorkspaceEventFrame | None:
        oldest = self.oldest_sequence
        if cursor < oldest - 1:
            raise WorkspaceCursorExpired(cursor, oldest)
        index = cursor + 1 - oldest
        frame = self._window.get(index)
        if frame is None:
            return None
        if frame.sequence != cursor + 1:
            raise WorkspaceCursorExpired(cursor, oldest)
        return frame

    def detach(self, subscriber: ReplaySubscriber) -> None:
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
        self._window.close()
