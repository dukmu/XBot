"""Application-owned routing for live client events.

Feature services publish their own event payloads and register their own
waiters. Transports install one live sink through ``install`` (which
returns its disposer) instead of discovering every
feature plugin that may need a client response.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from XBotv2.application.contracts import ClientEventSink, InteractionWaiterPort
from XBotv2.application.contracts import ClientEventsPort
from XBotv2.interactions.contracts import (
    InteractionNotPending,
    InteractionReceipt,
    InteractionRegistration,
    InteractionRequest,
    InteractionResolution,
)


class ClientEventRouter(ClientEventsPort):
    """Route client events without coupling transports to feature services."""

    def __init__(self, parent: "ClientEventRouter | None" = None) -> None:
        self._parent = parent
        self._sink: ClientEventSink | None = None
        self._interactions: dict[str, InteractionRegistration] = {}
        self._pending: dict[str, InteractionRequest] = {}

    def install(self, sink: ClientEventSink | None) -> Callable[[], None]:
        """Install one live sink and return its disposer.

        The installer owns the disposer, so restoring the previous sink is a
        single idempotent call instead of a manual save/restore dance on a
        shared field.
        """
        previous = self._sink
        self._sink = sink

        def dispose() -> None:
            if self._sink is sink:
                self._sink = previous

        return dispose

    async def request(
        self,
        event: InteractionRequest,
    ) -> InteractionResolution | None:
        if self._sink is None:
            if self._parent is not None:
                return await self._parent.request(event)
            return None
        self._registration_for(event)
        request_id = event.interaction_id
        if request_id in self._pending:
            raise InteractionNotPending(
                f"Duplicate pending interaction request: {request_id}"
            )
        self._pending[request_id] = event
        try:
            return await self._sink(event)
        finally:
            self._pending.pop(request_id, None)

    def register_interaction(
        self,
        registration: InteractionRegistration,
    ) -> Callable[[], bool]:
        if registration.kind in self._interactions:
            raise ValueError(
                f"interaction already registered: {registration.kind}"
            )
        if not registration.resolution_types:
            raise ValueError(
                f"interaction {registration.kind!r} has no resolution types"
            )
        self._interactions[registration.kind] = registration

        return partial(self._unregister_interaction, registration)

    def _unregister_interaction(self, registration: InteractionRegistration) -> bool:
        if self._interactions.get(registration.kind) is not registration:
            return False
        self._interactions.pop(registration.kind)
        return True

    def _registration_for(
        self,
        request: InteractionRequest,
    ) -> InteractionRegistration:
        registration = self._interactions.get(request.kind)
        if registration is None:
            raise TypeError(f"Unsupported interaction request kind: {request.kind!r}")
        if not isinstance(request, registration.request_type):
            raise TypeError(
                f"Interaction {request.kind!r} requires "
                f"{registration.request_type.__name__}, got {type(request).__name__}"
            )
        return registration

    def _pending_registration(
        self,
        interaction_id: str,
    ) -> tuple[InteractionRequest, InteractionRegistration]:
        request = self._pending.get(interaction_id)
        if request is None:
            raise InteractionNotPending(
                f"No live interaction request: {interaction_id}"
            )
        return request, self._registration_for(request)

    @staticmethod
    def _validate_resolution(
        registration: InteractionRegistration,
        resolution: InteractionResolution,
    ) -> None:
        if not isinstance(resolution, registration.resolution_types):
            expected = ", ".join(
                item.__name__ for item in registration.resolution_types
            )
            raise TypeError(
                f"Interaction {registration.kind!r} requires resolution "
                f"{expected}, got {type(resolution).__name__}"
            )

    def waiter_for(self, request: InteractionRequest) -> InteractionWaiterPort:
        return self._registration_for(request).waiter

    def timeout_for(self, request: InteractionRequest) -> float | None:
        return self._registration_for(request).timeout_seconds(request)

    def resolve(
        self,
        interaction_id: str,
        resolution: InteractionResolution,
    ) -> InteractionReceipt:
        _request, registration = self._pending_registration(interaction_id)
        self._validate_resolution(registration, resolution)
        registration.waiter.resolve(interaction_id, resolution)
        return InteractionReceipt(
            interaction_id=interaction_id,
            resolution=resolution,
            pending_ids=tuple(self.pending_request_ids()),
        )

    def cancel(
        self,
        interaction_id: str,
        reason: str,
        *,
        expected_kind: str | None = None,
    ) -> InteractionReceipt:
        request, registration = self._pending_registration(interaction_id)
        if expected_kind is not None and request.kind != expected_kind:
            raise TypeError(
                f"Interaction {interaction_id!r} is {request.kind!r}, "
                f"not {expected_kind!r}"
            )
        resolution = registration.waiter.cancel(interaction_id, reason)
        self._validate_resolution(registration, resolution)
        return InteractionReceipt(
            interaction_id=interaction_id,
            resolution=resolution,
            pending_ids=tuple(self.pending_request_ids()),
        )

    def recorded_event(
        self,
        request: InteractionRequest,
        resolution: InteractionResolution,
    ):
        registration = self._registration_for(request)
        self._validate_resolution(registration, resolution)
        receipt = InteractionReceipt(
            interaction_id=request.interaction_id,
            resolution=resolution,
            pending_ids=tuple(self.pending_request_ids()),
        )
        return registration.recorded_event(receipt)

    def pending_request_ids(self) -> list[str]:
        pending: list[str] = []
        for registration in self._interactions.values():
            pending.extend(registration.waiter.pending_request_ids())
        return pending

    def pending_interactions(self) -> list[InteractionRequest]:
        """Payloads of the requests this router is still waiting on."""
        pending_ids = set(self.pending_request_ids())
        return [
            request for interaction_id, request in self._pending.items()
            if interaction_id in pending_ids
        ]
