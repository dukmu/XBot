"""Runtime identity and filesystem API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from xcore import Context

from XBotv2.core.paths import RuntimePaths, SessionPaths

if TYPE_CHECKING:
    from XBotv2.core.loop import LoopSettings


class EngineView(Protocol):
    settings: "LoopSettings"
    context_window: int


@runtime_checkable
class SessionExecutionContext(Protocol):
    """Typed host-owned scope supplied to session capability operations."""

    session_id: str
    thread_id: str
    provider_name: str
    paths: RuntimePaths
    workspace_root: str
    interactive: bool
    turn_lock: asyncio.Lock
    engine: EngineView
    services: Context


@dataclass
class SessionInfo:
    session_id: str
    thread_id: str
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"


__all__ = [
    "EngineView",
    "RuntimePaths",
    "SessionExecutionContext",
    "SessionInfo",
    "SessionPaths",
]
