"""Typed thread metadata and its durable mutation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, Self

from pydantic import Field, field_validator

from XBotv2.core.state import JsonStateModel
from XBotv2.core.tools import JsonObject, json_object

THREAD_METADATA_SCHEMA_VERSION = 1


class ThreadMetadata(JsonStateModel):
    schema_version: Literal[1] = THREAD_METADATA_SCHEMA_VERSION
    agent: str = ""
    agent_definition: dict[str, object] | None = None
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    parent_thread_id: str = ""
    workspace_root: str = ""
    title: str = ""

    @field_validator("agent_definition", mode="before")
    @classmethod
    def _validate_definition(cls, value: object) -> JsonObject | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("agent_definition must be an object or null")
        return json_object(value)

    @classmethod
    def from_state(cls, value: Mapping[str, object]) -> "ThreadMetadata":
        return cls.model_validate({"schema_version": 1, **value})

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        expected = set(cls.model_fields)
        if set(value) != expected:
            missing = sorted(expected - set(value))
            unknown = sorted(set(value) - expected)
            raise ValueError(
                "ThreadMetadata fields mismatch; "
                f"missing={missing}, unknown={unknown}"
            )
        return cls.model_validate(dict(value))

    def to_state(self) -> JsonObject:
        value = self.to_dict()
        value.pop("schema_version")
        return value


class ThreadMetadataSink(Protocol):
    def save(self, metadata: ThreadMetadata) -> None: ...


class ThreadMetadataState:
    """Own one immutable metadata value and persist every replacement."""

    def __init__(
        self,
        value: ThreadMetadata | None = None,
        *,
        sink: ThreadMetadataSink | None = None,
    ) -> None:
        self._value = value or ThreadMetadata()
        self._sink = sink

    @property
    def value(self) -> ThreadMetadata:
        return self._value

    def replace(self, value: ThreadMetadata) -> None:
        if value == self._value:
            return
        if self._sink is not None:
            self._sink.save(value)
        self._value = value

    def update(self, **values: object) -> None:
        self.replace(ThreadMetadata.model_validate({
            **self._value.to_dict(), **values,
        }))

__all__ = [
    "THREAD_METADATA_SCHEMA_VERSION",
    "ThreadMetadata",
    "ThreadMetadataSink",
    "ThreadMetadataState",
]
