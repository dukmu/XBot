"""Live approval coordination owned by the permission-request plugin."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class ApprovalNotPending(RuntimeError):
    pass


@dataclass
class ApprovalResult:
    request_id: str
    status: str
    decision: str = ""
    scope: str = "once"
    answer: str = ""
    reason: str = ""


class ApprovalWaiter:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalResult]] = {}

    def register(self, request_id: str) -> asyncio.Future[ApprovalResult]:
        if request_id in self._pending:
            raise ApprovalNotPending(f"Duplicate pending approval: {request_id}")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    async def wait_registered(
        self,
        request_id: str,
        future: asyncio.Future[ApprovalResult],
        timeout_seconds: float | None,
    ) -> ApprovalResult:
        if self._pending.get(request_id) is not future:
            raise ApprovalNotPending(f"No matching live approval: {request_id}")
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            return ApprovalResult(request_id, "timeout", reason="timeout")
        finally:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)

    def answer(
        self,
        request_id: str,
        *,
        decision: str,
        scope: str = "once",
    ) -> ApprovalResult:
        return self._resolve(ApprovalResult(
            request_id,
            "answered",
            decision=decision,
            scope=scope,
        ))

    def cancel(self, request_id: str, reason: str = "cancelled") -> ApprovalResult:
        return self._resolve(ApprovalResult(
            request_id,
            "cancelled",
            reason=reason,
        ))

    def cancel_all(self, reason: str = "cancelled") -> list[ApprovalResult]:
        results = []
        for request_id in list(self._pending):
            try:
                results.append(self.cancel(request_id, reason))
            except ApprovalNotPending:
                pass
        return results

    def pending_request_ids(self) -> list[str]:
        return list(self._pending)

    def is_pending(self, request_id: str) -> bool:
        return request_id in self._pending

    def _resolve(self, result: ApprovalResult) -> ApprovalResult:
        future = self._pending.get(result.request_id)
        if future is None:
            raise ApprovalNotPending(
                f"No live approval request: {result.request_id}"
            )
        if not future.done():
            future.set_result(result)
        return result
