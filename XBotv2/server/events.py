"""Runtime inventory of capability-owned server event contracts.

Core wire events stay in :mod:`XBotv2.protocol.models`. Capability plugins
declare their own stream event types through :class:`ServerEvents` and register
the owning DTO so the server can validate and document them without protocol
owning a central inventory of business events.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from XBotv2.protocol.models import KNOWN_SERVER_EVENT_TYPES, WireModel

_DTO = TypeVar("_DTO", bound=type[WireModel])


class ServerEvents:
    """Runtime event contract registry provided by the server composition root."""

    def __init__(self) -> None:
        self._models: dict[str, type[WireModel]] = {}

    def register(self, event_type: str, dto: _DTO) -> Callable[[], None]:
        """Declare a capability event type and its DTO; returns a disposer.

        Registration is an effect: unload must dispose it. Re-registering a
        live type or claiming a protocol-owned core type is a composition
        error and raises.
        """
        if event_type in self._models:
            raise RuntimeError(f"server_events collision: {event_type}")
        if event_type in KNOWN_SERVER_EVENT_TYPES:
            raise RuntimeError(
                f"server_events collision: {event_type} is a protocol core event"
            )
        self._models[event_type] = dto
        return lambda: self._models.pop(event_type, None)

    def data_model(self, event_type: str) -> type[WireModel] | None:
        return self._models.get(event_type)

    def types(self) -> tuple[str, ...]:
        return tuple(self._models)

    def validate(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Normalize one event payload against its registered DTO.

        Unregistered event types pass through unchanged; ``ServerEvent``
        validation still applies for protocol core events.
        """
        model = self._models.get(event_type)
        if model is None:
            return data or {}
        return model.model_validate(data or {}).model_dump(exclude_unset=True)