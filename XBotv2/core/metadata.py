"""Typed thread metadata and the bus event that announces every change."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from xcore import Context
from xcore.service import Service


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
    def from_state(cls, value: Mapping[str, JsonValue]) -> "ThreadMetadata":
        return cls.model_validate({"schema_version": 1, **value})


THREAD_METADATA_CHANGED = "metadata/changed"


@dataclass(frozen=True, slots=True)
class ThreadMetadataChanged:
    """One accepted metadata change, published as a general fact.

    The publisher interprets no field: subscribers decide which fields matter
    to them and receive the complete previous and current values.
    """

    session_id: str
    thread_id: str
    previous: ThreadMetadata
    current: ThreadMetadata


class ThreadMetadataState(Service):
    """Owns one thread's metadata; every accepted change is a bus event.

    The instance is provided on the owning context as ``thread_metadata`` at
    construction and is released with the owning fiber; identity is permanent,
    so callers never rebind or rebind-proof it. ``replace``/``update`` are the
    only write paths and announce ``metadata/changed`` with the whole previous
    and current values before returning, so a durable writer that awaited the
    write has already handed the change to every subscriber.
    """

    name = "thread_metadata"

    def __init__(
        self,
        ctx: Context,
        *,
        session_id: str = "",
        thread_id: str = "",
        value: ThreadMetadata | None = None,
    ) -> None:
        super().__init__(ctx, name=self.name)
        self._session_id = session_id
        self._thread_id = thread_id
        self._value = value or ThreadMetadata()

    @property
    def value(self) -> ThreadMetadata:
        return self._value

    async def replace(self, value: ThreadMetadata) -> None:
        if value == self._value:
            return
        previous = self._value
        self._value = value
        await self.ctx.emit(
            THREAD_METADATA_CHANGED,
            ThreadMetadataChanged(
                session_id=self._session_id,
                thread_id=self._thread_id,
                previous=previous,
                current=value,
            ),
        )

    async def update(self, **values: JsonValue) -> None:
        await self.replace(ThreadMetadata.model_validate({
            **self._value.model_dump(), **values,
        }))


__all__ = [
    "THREAD_METADATA_CHANGED",
    "THREAD_METADATA_SCHEMA_VERSION",
    "ThreadMetadata",
    "ThreadMetadataChanged",
    "ThreadMetadataState",
]
