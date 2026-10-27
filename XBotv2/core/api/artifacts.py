"""Provider-neutral artifact identities and storage contract."""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ArtifactKind(str, Enum):
    MEDIA = "media"
    ATTACHMENT = "attachments"
    TOOL_RESULT = "tool_results"
    CONTEXT = "context"
    BROWSER = "browser"


class ArtifactRef(BaseModel):
    """Logical artifact identity independent of the physical file layout."""

    id: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    name: str = ""
    kind: ArtifactKind = ArtifactKind.ATTACHMENT
    size: int = Field(default=0, ge=0)
    sha256: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactStorePort(Protocol):
    async def reference_records(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        through_seq: int | None = None,
        record_ids: Sequence[str] | None = None,
        kind: ArtifactKind = ArtifactKind.CONTEXT,
        name: str = "records.jsonl",
    ) -> ArtifactRef: ...

    async def reference(self, identity: str) -> ArtifactRef: ...

    def using(self) -> AbstractAsyncContextManager[None]: ...

    def pin(self, artifact: ArtifactRef | str) -> AbstractAsyncContextManager[str]: ...

    async def expose(self, artifact: ArtifactRef | str, resources: AsyncExitStack) -> str: ...

    async def handoff(self) -> AsyncExitStack: ...

    def resolve_model_path(self, value: str) -> str: ...

    async def reference_path(self, value: str) -> ArtifactRef | None: ...

    def managed_roots(self) -> tuple[str, ...]: ...

    def resolve_path(self, path: str) -> AbstractAsyncContextManager[str]: ...

    async def selection_stream(self, artifact: ArtifactRef | str) -> str | None: ...

    def read_records(self, artifact: ArtifactRef | str) -> AsyncIterator[dict]: ...

    def spool_file(self) -> AbstractAsyncContextManager[Path]: ...

    def publication(self) -> AbstractAsyncContextManager[None]: ...

    def prepare_stream(
        self,
        kind: ArtifactKind,
        chunks: AsyncIterable[bytes],
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        suffix: str = "",
    ) -> AbstractAsyncContextManager[ArtifactRef]: ...

    async def put_stream(
        self,
        kind: ArtifactKind,
        chunks: AsyncIterable[bytes],
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        suffix: str = "",
    ) -> ArtifactRef: ...

    async def adopt_file(
        self,
        kind: ArtifactKind,
        source: Path,
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        suffix: str = "",
    ) -> ArtifactRef: ...

    def read_chunks(
        self,
        artifact: ArtifactRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 256 * 1024,
    ) -> AsyncIterator[bytes]: ...

    async def export_reference(
        self,
        artifact: ArtifactRef,
        destination: Path,
        *,
        supplied_streams: Sequence[str] = (),
    ) -> None: ...

    async def import_reference(self, source: Path, artifact: ArtifactRef) -> None: ...

    async def export_file(self, artifact: ArtifactRef, destination: Path) -> None: ...

    async def import_file(self, source: Path, artifact: ArtifactRef) -> None: ...

    async def put(
        self,
        kind: ArtifactKind,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        suffix: str = "",
    ) -> ArtifactRef: ...

    async def read(self, artifact: ArtifactRef | str) -> bytes: ...

    async def exists(self, artifact: ArtifactRef | str) -> bool: ...

    def model_path(self, artifact: ArtifactRef | str) -> str: ...
