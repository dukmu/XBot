"""Versioned persistence envelopes for canonical domain values."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.agentloop.contracts import InboxItem
from XBotv2.core.history import (
    DurableEventRecorded,
    MessageAppended,
    SurfaceReplaced,
    TrajectoryEntry,
)

TRAJECTORY_SCHEMA_VERSION = 1
INBOX_SCHEMA_VERSION = 1


class PersistenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoredTrajectoryRecord(PersistenceRecord):
    schema_version: Literal[1] = TRAJECTORY_SCHEMA_VERSION
    entry: Annotated[
        MessageAppended | SurfaceReplaced | DurableEventRecorded,
        Field(discriminator="kind"),
    ]


class InboxSnapshot(PersistenceRecord):
    version: Literal[1] = INBOX_SCHEMA_VERSION
    items: tuple[InboxItem, ...]


__all__ = [
    "InboxSnapshot",
    "StoredTrajectoryRecord",
]
