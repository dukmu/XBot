"""Versioned persistence envelopes for canonical domain values."""

from __future__ import annotations
from dataclasses import dataclass

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from XBotv2.agentloop.api.contracts import InboxMutation
from XBotv2.core.api.history import DurableEventRecorded, MessageAppended, SurfaceReplaced

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


class StoredInboxRecord(PersistenceRecord):
    version: Literal[1] = INBOX_SCHEMA_VERSION
    change: Annotated[InboxMutation, Field(discriminator="kind")]




@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: bytes
    media_type: str
    name: str = ""
