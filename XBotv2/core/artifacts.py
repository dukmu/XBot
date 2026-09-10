"""Provider-neutral artifact identities and storage contract."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

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


class ImageContent(BaseModel):
    """A session-relative image artifact attached to a message."""

    path: str
    media_type: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactStorePort(Protocol):
    def put(
        self,
        kind: ArtifactKind,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        suffix: str = "",
    ) -> ArtifactRef: ...

    def read(self, artifact: ArtifactRef | str) -> bytes: ...

    def exists(self, artifact: ArtifactRef | str) -> bool: ...

    def model_path(self, artifact: ArtifactRef | str) -> str: ...


__all__ = ["ArtifactKind", "ArtifactRef", "ArtifactStorePort", "ImageContent"]
