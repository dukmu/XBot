"""In-memory client interaction coordination for a live engine turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


class InteractionDisconnected(RuntimeError):
    """Raised when the live client disconnects during an interaction."""


class InteractionNotPending(RuntimeError):
    """Raised when a response targets no live interaction request."""


@dataclass
class InteractionResult:
    request_id: str
    status: str
    answer: Any = None
    decision: str = ""
    scope: str = "once"
    reason: str = ""


class InteractionWaiter:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[InteractionResult]] = {}

    def register(self, request_id: str) -> asyncio.Future[InteractionResult]:
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
    ) -> InteractionResult:
        future = self.register(request_id)
        return await self.wait_registered(request_id, future, timeout_seconds)

    async def wait_registered(
        self,
        request_id: str,
        future: asyncio.Future[InteractionResult],
        timeout_seconds: float | None,
    ) -> InteractionResult:
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
            return InteractionResult(
                request_id=request_id,
                status="timeout",
                reason="timeout",
            )
        finally:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)

    def _resolve(self, request_id: str, result: InteractionResult) -> InteractionResult:
        future = self._pending.get(request_id)
        if future is None:
            raise InteractionNotPending(f"No live interaction request: {request_id}")
        if not future.done():
            future.set_result(result)
        return result

    def answer(self, request_id: str, *, answer: Any = None, decision: str = "", scope: str = "once") -> InteractionResult:
        return self._resolve(request_id, InteractionResult(
            request_id=request_id, status="answered", answer=answer, decision=decision, scope=scope,
        ))

    def cancel(self, request_id: str, reason: str = "cancelled") -> InteractionResult:
        return self._resolve(request_id, InteractionResult(
            request_id=request_id, status="cancelled", reason=reason,
        ))

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResult]:
        results = []
        for request_id in list(self._pending):
            try:
                results.append(self.cancel(request_id, reason))
            except InteractionNotPending:
                continue
        return results

    def is_pending(self, request_id: str) -> bool:
        return request_id in self._pending

    def pending_request_ids(self) -> list[str]:
        return list(self._pending)


UserInputDisconnected = InteractionDisconnected
