"""Provider-neutral artifact identities and storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Mapping
from typing import Protocol


class ArtifactKind(str, Enum):
    MEDIA = "media"
    ATTACHMENT = "attachments"
    TOOL_RESULT = "tool_results"
    CONTEXT = "context"
    BROWSER = "browser"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Logical artifact identity independent of the physical file layout."""

    id: str
    media_type: str = "application/octet-stream"
    name: str = ""
    kind: ArtifactKind = ArtifactKind.ATTACHMENT
    size: int = 0
    sha256: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ArtifactRef":
        expected = {"id", "kind", "media_type", "name", "size", "sha256"}
        if set(value) != expected:
            raise ValueError(
                "ArtifactRef fields must be exactly: "
                + ", ".join(sorted(expected))
            )
        try:
            kind = ArtifactKind(value["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid artifact kind: {value['kind']!r}") from exc
        if not isinstance(value["id"], str) or not value["id"]:
            raise TypeError("ArtifactRef.id must be a non-empty string")
        if not isinstance(value["media_type"], str):
            raise TypeError("ArtifactRef.media_type must be a string")
        if not isinstance(value["name"], str):
            raise TypeError("ArtifactRef.name must be a string")
        if not isinstance(value["size"], int) or value["size"] < 0:
            raise TypeError("ArtifactRef.size must be a non-negative integer")
        if not isinstance(value["sha256"], str):
            raise TypeError("ArtifactRef.sha256 must be a string")
        return cls(
            id=value["id"],
            kind=kind,
            media_type=value["media_type"],
            name=value["name"],
            size=value["size"],
            sha256=value["sha256"],
        )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "media_type": self.media_type,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
        }


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


__all__ = ["ArtifactKind", "ArtifactRef", "ArtifactStorePort"]
