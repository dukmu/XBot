"""Cursor subscriptions over the shared workspace replay window."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from XBotv2.workspaces.api.events import WorkspaceEventFrame


@dataclass(slots=True, eq=False)
class ReplaySubscriber:
    wakeups: asyncio.Queue[None]
    closed: bool = False


class ReplaySubscription(AsyncIterator[WorkspaceEventFrame]):
    def __init__(self, stream, subscriber: ReplaySubscriber, cursor: int) -> None:
        self._stream = stream
        self._subscriber = subscriber
        self._cursor = cursor
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> WorkspaceEventFrame:
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
        if not self._closed:
            self._closed = True
            self._stream.detach(self._subscriber)
