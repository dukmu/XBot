"""In-memory client interaction coordination for a live engine turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


class InteractionCancelled(RuntimeError):
    """Raised when a live interaction is cancelled by the client."""


class InteractionDisconnected(RuntimeError):
    """Raised when the live client disconnects during an interaction."""


class InteractionNotPending(RuntimeError):
    """Raised when a response targets no live interaction request."""


@dataclass
class InteractionResult:
    """Result returned to a waiting live interaction."""

    request_id: str
    status: str
    answer: Any = None
    decision: str = ""
    reason: str = ""


class InteractionWaiter:
    """Tracks live interaction requests for one engine instance."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[InteractionResult]] = {}

    async def wait(self, request_id: str, timeout_seconds: float | None) -> InteractionResult:
        """Wait for a matching answer, timeout, or cancellation."""
        if request_id in self._pending:
            raise InteractionNotPending(f"Duplicate pending interaction request: {request_id}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionResult] = loop.create_future()
        self._pending[request_id] = future
        try:
            if timeout_seconds is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout=float(timeout_seconds))
            except asyncio.TimeoutError:
                return InteractionResult(
                    request_id=request_id,
                    status="timeout",
                    reason="timeout",
                )
        finally:
            self._pending.pop(request_id, None)

    def answer(
        self,
        request_id: str,
        *,
        answer: Any = None,
        decision: str = "",
    ) -> InteractionResult:
        """Resolve one pending request with a client answer."""
        future = self._pending.get(request_id)
        if future is None:
            raise InteractionNotPending(f"No live interaction request: {request_id}")
        result = InteractionResult(
            request_id=request_id,
            status="answered",
            answer=answer,
            decision=decision,
        )
        if not future.done():
            future.set_result(result)
        return result

    def cancel(self, request_id: str, reason: str = "cancelled") -> InteractionResult:
        """Resolve one pending request as cancelled."""
        future = self._pending.get(request_id)
        if future is None:
            raise InteractionNotPending(f"No live interaction request: {request_id}")
        result = InteractionResult(
            request_id=request_id,
            status="cancelled",
            reason=reason,
        )
        if not future.done():
            future.set_result(result)
        return result

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResult]:
        """Cancel every live request and return the emitted results."""
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


UserInputCancelled = InteractionCancelled
UserInputDisconnected = InteractionDisconnected
UserInputNotPending = InteractionNotPending
UserInputResult = InteractionResult
UserInputWaiter = InteractionWaiter
