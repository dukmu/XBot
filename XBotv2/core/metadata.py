"""Typed thread metadata and the bus event that announces every change."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from xcore import Context
from xcore.service import Service
from XBotv2.core.domain import ResolvedRuntimeSelection


THREAD_METADATA_SCHEMA_VERSION = 1


class ThreadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = THREAD_METADATA_SCHEMA_VERSION
    runtime_selection: ResolvedRuntimeSelection
    parent_thread_id: str = ""
    workspace_root: str = ""
    title: str = ""

    def with_default_title(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> "ThreadMetadata":
        """Return metadata with the startup title when none was stored."""
        if self.title:
            return self
        if self.parent_thread_id:
            title = self.runtime_selection.agent_name
        else:
            title = session_id or thread_id
        return self.model_copy(update={"title": title})


THREAD_METADATA_CHANGED = "metadata/changed"
THREAD_METADATA_INITIALIZED = "metadata/initialized"


@dataclass(frozen=True, slots=True)
class MetadataUninitialized:
    """No effective thread metadata has been resolved for this runtime."""


@dataclass(frozen=True, slots=True)
class MetadataReady:
    metadata: ThreadMetadata


ThreadMetadataLifecycle: TypeAlias = MetadataUninitialized | MetadataReady


class ThreadMetadataNotInitialized(RuntimeError):
    """A consumer tried to read thread metadata before runtime resolution."""


@dataclass(frozen=True, slots=True)
class ThreadMetadataInitialized:
    session_id: str
    thread_id: str
    current: ThreadMetadata


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
    """Own one thread's metadata lifecycle and publish each state transition.

    A runtime starts without effective metadata. The Agent resolver or
    persistence hydration initializes it exactly once; subsequent replacements
    publish the complete previous and current values.
    """

    name = "thread_metadata"

    def __init__(
        self,
        ctx: Context,
        *,
        session_id: str,
        thread_id: str,
    ) -> None:
        super().__init__(ctx, name=self.name)
        self._session_id = session_id
        self._thread_id = thread_id
        self._state: ThreadMetadataLifecycle = MetadataUninitialized()

    @property
    def value(self) -> ThreadMetadata:
        if isinstance(self._state, MetadataUninitialized):
            raise ThreadMetadataNotInitialized(
                f"Thread metadata is not initialized for "
                f"{self._session_id}/{self._thread_id}"
            )
        return self._state.metadata

    @property
    def lifecycle(self) -> ThreadMetadataLifecycle:
        return self._state

    async def initialize(self, value: ThreadMetadata) -> None:
        if not isinstance(self._state, MetadataUninitialized):
            raise RuntimeError(
                f"Thread metadata already initialized for "
                f"{self._session_id}/{self._thread_id}"
            )
        value = value.with_default_title(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        self._state = MetadataReady(value)
        await self.ctx.emit(
            THREAD_METADATA_INITIALIZED,
            ThreadMetadataInitialized(
                session_id=self._session_id,
                thread_id=self._thread_id,
                current=value,
            ),
        )

    async def replace(self, value: ThreadMetadata) -> None:
        value = value.with_default_title(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        previous = self.value
        if value == previous:
            return
        self._state = MetadataReady(value)
        await self.ctx.emit(
            THREAD_METADATA_CHANGED,
            ThreadMetadataChanged(
                session_id=self._session_id,
                thread_id=self._thread_id,
                previous=previous,
                current=value,
            ),
        )

    async def replace_runtime_selection(
        self,
        selection: ResolvedRuntimeSelection,
    ) -> None:
        await self.replace(self.value.model_copy(update={"runtime_selection": selection}))

    async def replace_title(self, title: str) -> None:
        await self.replace(self.value.model_copy(update={"title": title}))


__all__ = [
    "THREAD_METADATA_CHANGED",
    "THREAD_METADATA_INITIALIZED",
    "THREAD_METADATA_SCHEMA_VERSION",
    "MetadataReady",
    "MetadataUninitialized",
    "ThreadMetadata",
    "ThreadMetadataChanged",
    "ThreadMetadataInitialized",
    "ThreadMetadataLifecycle",
    "ThreadMetadataNotInitialized",
    "ThreadMetadataState",
]
