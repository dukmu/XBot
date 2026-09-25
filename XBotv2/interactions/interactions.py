"""In-memory client interaction coordination for a live engine turn."""

from __future__ import annotations

import asyncio

from XBotv2.interactions.contracts import (
    InteractionResolution,
    InteractionWaiterPort,
    InteractionNotPending,
    ResolutionFactory,
)

class InteractionWaiter(InteractionWaiterPort):
    def __init__(
        self,
        *,
        timed_out: ResolutionFactory,
        cancelled: ResolutionFactory,
    ) -> None:
        self._timed_out = timed_out
        self._cancelled = cancelled
        self._pending: dict[str, asyncio.Future[InteractionResolution]] = {}

    def register(self, request_id: str) -> asyncio.Future[InteractionResolution]:
        """Register a request before exposing it to a live client."""
        if request_id in self._pending:
            raise InteractionNotPending(
                f"Duplicate pending interaction request: {request_id}"
            )
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    async def wait(
        self,
        request_id: str,
        timeout_seconds: float | None,
    ) -> InteractionResolution:
        future = self.register(request_id)
        return await self.wait_registered(request_id, future, timeout_seconds)

    async def wait_registered(
        self,
        request_id: str,
        future: asyncio.Future[InteractionResolution],
        timeout_seconds: float | None,
    ) -> InteractionResolution:
        """Wait for a request previously created by :meth:`register`."""
        if self._pending.get(request_id) is not future:
            raise InteractionNotPending(
                f"No matching live interaction request: {request_id}"
            )
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            return self._timed_out("timeout")
        finally:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)

    def resolve(
        self,
        request_id: str,
        resolution: InteractionResolution,
    ) -> InteractionResolution:
        future = self._pending.get(request_id)
        if future is None:
            raise InteractionNotPending(f"No live interaction request: {request_id}")
        if future.done():
            raise InteractionNotPending(
                f"Interaction request {request_id!r} has already been resolved"
            )
        future.set_result(resolution)
        return resolution

    def cancel(self, request_id: str, reason: str = "cancelled") -> InteractionResolution:
        return self.resolve(request_id, self._cancelled(reason))

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResolution]:
        results: list[InteractionResolution] = []
        for request_id in list(self._pending):
            try:
                results.append(self.cancel(request_id, reason))
            except InteractionNotPending:
                continue
        return results

    def pending_request_ids(self) -> list[str]:
        return [
            request_id for request_id, future in self._pending.items()
            if not future.done()
        ]
