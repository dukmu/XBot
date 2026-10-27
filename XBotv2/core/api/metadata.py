"""Typed thread metadata and the bus event that announces every change."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, Protocol
from pydantic import BaseModel, ConfigDict
from XBotv2.core.api.domain import ResolvedRuntimeSelection


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


class Metadata(Protocol):
    """Effective thread metadata and its accepted transitions."""

    @property
    def value(self) -> ThreadMetadata: ...
    @property
    def lifecycle(self) -> ThreadMetadataLifecycle: ...
    async def initialize(self, value: ThreadMetadata) -> None: ...
    async def replace(self, value: ThreadMetadata) -> None: ...
    async def replace_runtime_selection(self, selection: ResolvedRuntimeSelection) -> None: ...
    async def replace_title(self, title: str) -> None: ...
