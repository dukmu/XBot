"""Typed notifications owned by application composition."""

from __future__ import annotations

from dataclasses import dataclass
from pydantic import BaseModel

from XBotv2.core.metadata import ThreadMetadata
from XBotv2.session.contracts import SessionRuntimeState


APPLICATION_INITIALIZED = "session/init"
RUNTIME_EVENT = "runtime/event"


@dataclass(frozen=True, slots=True)
class ApplicationInitialized:
    session: SessionRuntimeState
    metadata: ThreadMetadata


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event: BaseModel


__all__ = [
    "APPLICATION_INITIALIZED",
    "ApplicationInitialized",
    "RUNTIME_EVENT",
    "RuntimeEvent",
]
