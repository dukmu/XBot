"""In-memory client interaction coordination for a live engine turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


class UserInputCancelled(RuntimeError):
    """Raised when a live user-input request is cancelled by the client."""


class UserInputDisconnected(RuntimeError):
    """Raised when the live client disconnects during ask_user."""


class UserInputNotPending(RuntimeError):
    """Raised when a response targets no live user-input request."""


@dataclass
class UserInputResult:
    """Result returned to the waiting ask_user tool call."""

    request_id: str
    status: str
    answer: Any = None
    reason: str = ""


class UserInputWaiter:
    """Tracks live ask_user requests for one engine instance."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[UserInputResult]] = {}

    async def wait(self, request_id: str, timeout_seconds: float | None) -> UserInputResult:
        """Wait for a matching answer, timeout, or cancellation."""
        if request_id in self._pending:
            raise UserInputNotPending(f"Duplicate pending user input request: {request_id}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[UserInputResult] = loop.create_future()
        self._pending[request_id] = future
        try:
            if timeout_seconds is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout=float(timeout_seconds))
            except asyncio.TimeoutError:
                return UserInputResult(
                    request_id=request_id,
                    status="timeout",
                    reason="timeout",
                )
        finally:
            self._pending.pop(request_id, None)

    def answer(self, request_id: str, answer: Any) -> UserInputResult:
        """Resolve one pending request with a client answer."""
        future = self._pending.get(request_id)
        if future is None:
            raise UserInputNotPending(f"No live user input request: {request_id}")
        result = UserInputResult(
            request_id=request_id,
            status="answered",
            answer=answer,
        )
        if not future.done():
            future.set_result(result)
        return result

    def cancel(self, request_id: str, reason: str = "cancelled") -> UserInputResult:
        """Resolve one pending request as cancelled."""
        future = self._pending.get(request_id)
        if future is None:
            raise UserInputNotPending(f"No live user input request: {request_id}")
        result = UserInputResult(
            request_id=request_id,
            status="cancelled",
            reason=reason,
        )
        if not future.done():
            future.set_result(result)
        return result

    def cancel_all(self, reason: str = "cancelled") -> list[UserInputResult]:
        """Cancel every live request and return the emitted results."""
        results = []
        for request_id in list(self._pending):
            try:
                results.append(self.cancel(request_id, reason))
            except UserInputNotPending:
                continue
        return results

    def is_pending(self, request_id: str) -> bool:
        return request_id in self._pending

    def pending_request_ids(self) -> list[str]:
        return list(self._pending)
