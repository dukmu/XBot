from __future__ import annotations
from collections.abc import Callable
from pydantic import BaseModel
"""Public service contract for live client interactions."""


import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel
from xcore.api.services import ServiceKey

if TYPE_CHECKING:
    from XBotv2.interactions.api.models import UserInputOption

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


INTERACTIONS = ServiceKey[InteractionsPort]("interactions")


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


class ClientEventSink(Protocol):
    async def __call__(
        self,
        event: InteractionRequest,
    ) -> InteractionResolution: ...


class ClientEventsPort(Protocol):
    def install(self, sink: ClientEventSink | None) -> Callable[[], None]: ...

    async def request(
        self,
        event: InteractionRequest,
    ) -> InteractionResolution | None: ...

    def register_interaction(
        self,
        registration: InteractionRegistration,
    ) -> Callable[[], bool]: ...

    def waiter_for(self, request: InteractionRequest) -> InteractionWaiterPort: ...

    def timeout_for(self, request: InteractionRequest) -> float | None: ...

    def resolve(
        self,
        interaction_id: str,
        resolution: InteractionResolution,
    ) -> InteractionReceipt: ...

    def cancel(
        self,
        interaction_id: str,
        reason: str,
        *,
        expected_kind: str | None = None,
    ) -> InteractionReceipt: ...

    def recorded_event(
        self,
        request: InteractionRequest,
        resolution: InteractionResolution,
    ) -> BaseModel: ...

    def pending_request_ids(self) -> list[str]: ...

    def pending_interactions(self) -> list[InteractionRequest]: ...



CLIENT_EVENTS: ServiceKey[ClientEventsPort] = ServiceKey("client_events")
