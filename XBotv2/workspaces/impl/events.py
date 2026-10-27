"""Bounded workspace event replay and fan-out."""

import asyncio
from collections import deque
from XBotv2.workspaces.api.events import WorkspaceCatalogChange, WorkspaceCursorExpired, WorkspaceEventFrame
from XBotv2.workspaces.impl.replay import ReplaySubscriber, ReplaySubscription, fan_out


class WorkspaceEventSubscription(ReplaySubscription[WorkspaceEventFrame]):
    def __init__(
        self,
        stream: "WorkspaceEventStream",
        subscriber: ReplaySubscriber[WorkspaceEventFrame],
        replay: tuple[WorkspaceEventFrame, ...],
        cursor: int,
    ) -> None:
        super().__init__(
            stream=stream,
            subscriber=subscriber,
            replay=replay,
            cursor=cursor,
            cursor_error=WorkspaceCursorExpired,
        )


class WorkspaceEventStream:
    """Bounded replay and live fan-out for committed catalog changes."""

    def __init__(self, *, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError("Workspace event capacity must be positive")
        self._capacity = capacity
        self._frames: deque[WorkspaceEventFrame] = deque(maxlen=capacity)
        self._subscribers: set[ReplaySubscriber[WorkspaceEventFrame]] = set()
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
        fan_out(self._subscribers, frame)
        return frame

    def subscribe(self, after: int) -> WorkspaceEventSubscription:
        if after < 0 or after > self._sequence:
            raise ValueError("Workspace event cursor is outside the current sequence")
        oldest = self.oldest_sequence
        if after < oldest - 1:
            raise WorkspaceCursorExpired(after, oldest)
        replay = tuple(frame for frame in self._frames if frame.sequence > after)
        subscriber = ReplaySubscriber(asyncio.Queue(maxsize=self._capacity))
        self._subscribers.add(subscriber)
        return WorkspaceEventSubscription(self, subscriber, replay, after)

    def detach(self, subscriber: ReplaySubscriber[WorkspaceEventFrame]) -> None:
        self._subscribers.discard(subscriber)
