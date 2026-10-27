"""Durable identities and storage failure contracts."""

from __future__ import annotations
from dataclasses import dataclass
from collections.abc import AsyncIterator
from pydantic import BaseModel, ConfigDict


class StreamCorrupt(ValueError):
    """A complete record or one of its references is invalid."""


class CommitUncertain(RuntimeError):
    """The log is durable but its index is unconfirmed; retry the original ID."""


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: AsyncIterator[bytes]
    media_type: str
    name: str
    size: int
