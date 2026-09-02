"""Typed thread metadata and its durable mutation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue


THREAD_METADATA_SCHEMA_VERSION = 1


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
    def from_state(cls, value: Mapping[str, object]) -> "ThreadMetadata":
        return cls.model_validate({"schema_version": 1, **value})

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
            **self._value.model_dump(), **values,
        }))

__all__ = [
    "THREAD_METADATA_SCHEMA_VERSION",
    "ThreadMetadata",
    "ThreadMetadataSink",
    "ThreadMetadataState",
]
