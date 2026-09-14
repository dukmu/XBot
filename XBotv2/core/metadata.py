"""Typed thread metadata and its observable mutation boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


THREAD_METADATA_SCHEMA_VERSION = 1

MetadataObserver = Callable[["ThreadMetadata", "ThreadMetadata"], None]


class ThreadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = THREAD_METADATA_SCHEMA_VERSION
    agent: str = ""
    agent_definition: dict[str, JsonValue] | None = None
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    parent_thread_id: str = ""
    workspace_root: str = ""
    title: str = ""

    @classmethod
    def from_state(cls, value: Mapping[str, JsonValue]) -> "ThreadMetadata":
        return cls.model_validate({"schema_version": 1, **value})


class ThreadMetadataState:
    """Owns one thread's metadata; every accepted change is observable.

    The instance identity is permanent: callers hold it for the lifetime of
    the runtime and mutate it in place, so nobody has to rebind or rebind-proof
    it. Observers receive the complete previous and current values and decide
    which fields matter to them.
    """

    __slots__ = ("_value", "_observers")

    def __init__(self, value: ThreadMetadata | None = None) -> None:
        self._value = value or ThreadMetadata()
        self._observers: list[MetadataObserver] = []

    @property
    def value(self) -> ThreadMetadata:
        return self._value

    def observe(self, observer: MetadataObserver) -> Callable[[], None]:
        """Subscribe to value changes; the returned callable unsubscribes."""

        self._observers.append(observer)

        def _dispose() -> None:
            try:
                self._observers.remove(observer)
            except ValueError:
                pass

        return _dispose

    def replace(self, value: ThreadMetadata) -> None:
        if value == self._value:
            return
        previous = self._value
        self._value = value
        for observer in tuple(self._observers):
            observer(previous, value)

    def update(self, **values: JsonValue) -> None:
        self.replace(ThreadMetadata.model_validate({
            **self._value.model_dump(), **values,
        }))


THREAD_METADATA_CHANGED = "metadata/changed"


@dataclass(frozen=True, slots=True)
class ThreadMetadataChanged:
    """One accepted metadata change, published as a general fact.

    Subscribers decide which fields matter to them; the publisher carries the
    whole previous and current values and interprets no field itself.
    """

    session_id: str
    thread_id: str
    previous: ThreadMetadata
    current: ThreadMetadata


__all__ = [
    "THREAD_METADATA_CHANGED",
    "THREAD_METADATA_SCHEMA_VERSION",
    "MetadataObserver",
    "ThreadMetadata",
    "ThreadMetadataChanged",
    "ThreadMetadataState",
]
