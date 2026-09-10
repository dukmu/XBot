"""Public service contract for live client interactions."""

from __future__ import annotations

import asyncio
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue


class InteractionNotPending(RuntimeError):
    """Raised when a response targets no live interaction request."""


class InteractionResult(BaseModel):
    request_id: str
    status: str
    answer: JsonValue = None
    decision: str = ""
    scope: str = "once"
    reason: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


class InteractionWaiterPort(Protocol):
    def register(self, request_id: str) -> asyncio.Future[InteractionResult]: ...

    async def wait_registered(
        self,
        request_id: str,
        pending: asyncio.Future[InteractionResult],
        timeout_seconds: float | None,
    ) -> InteractionResult: ...

    def answer(self, request_id: str, **values: JsonValue) -> InteractionResult: ...

    def cancel(
        self,
        request_id: str,
        reason: str = "cancelled",
    ) -> InteractionResult: ...

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResult]: ...

    def pending_request_ids(self) -> list[str]: ...


class InteractionsPort(Protocol):
    def create_waiter(self) -> InteractionWaiterPort: ...

    async def request_user_input(
        self,
        question: str,
        *,
        options: list[dict[str, str]] | None = None,
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, JsonValue]: ...


__all__ = [
    "InteractionNotPending",
    "InteractionResult",
    "InteractionWaiterPort",
    "InteractionsPort",
]
