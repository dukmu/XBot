"""Public service contract for live client interactions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from XBotv2.interactions.protocol import UserInputOption

class InteractionNotPending(RuntimeError):
    """Raised when a response targets no live interaction request."""


class InteractionRequest(Protocol):
    """Common identity carried by a typed interaction request."""

    interaction_id: str
    kind: str
    resume_supported: bool


class InteractionResolution(Protocol):
    kind: str


ResolutionFactory = Callable[[str], InteractionResolution]


@dataclass(frozen=True, slots=True)
class InteractionReceipt:
    interaction_id: str
    resolution: InteractionResolution
    pending_ids: tuple[str, ...] = ()


InteractionRecordedFactory = Callable[[InteractionReceipt], BaseModel]
InteractionTimeout = Callable[[InteractionRequest], float | None]


@dataclass(frozen=True, slots=True)
class InteractionRegistration:
    """One feature-owned interaction route installed at composition time."""

    kind: str
    request_type: type
    resolution_types: tuple[type, ...]
    waiter: "InteractionWaiterPort"
    timeout_seconds: InteractionTimeout
    recorded_event: InteractionRecordedFactory


class InteractionWaiterPort(Protocol):
    def register(self, request_id: str) -> asyncio.Future[InteractionResolution]: ...

    async def wait_registered(
        self,
        request_id: str,
        pending: asyncio.Future[InteractionResolution],
        timeout_seconds: float | None,
    ) -> InteractionResolution: ...

    def resolve(
        self,
        request_id: str,
        resolution: InteractionResolution,
    ) -> InteractionResolution: ...

    def cancel(
        self,
        request_id: str,
        reason: str = "cancelled",
    ) -> InteractionResolution: ...

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResolution]: ...

    def pending_request_ids(self) -> list[str]: ...


class InteractionsPort(Protocol):
    def create_waiter(
        self,
        *,
        timed_out: ResolutionFactory,
        cancelled: ResolutionFactory,
    ) -> InteractionWaiterPort: ...

    async def request_user_input(
        self,
        question: str,
        *,
        options: tuple["UserInputOption", ...] = (),
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> InteractionResolution: ...


__all__ = [
    "InteractionNotPending",
    "InteractionResolution",
    "InteractionReceipt",
    "InteractionRecordedFactory",
    "InteractionRegistration",
    "InteractionRequest",
    "InteractionTimeout",
    "InteractionWaiterPort",
    "InteractionsPort",
    "ResolutionFactory",
]
