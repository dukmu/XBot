"""Provider-neutral artifact identities and storage contract."""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager
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


class ImageRef(BaseModel):
    """Logical image identity carried by a persisted message."""

    artifact_id: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactStorePort(Protocol):
    def spool_file(self) -> AbstractAsyncContextManager[Path]: ...

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
