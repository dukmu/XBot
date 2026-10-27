"""Narrow handle for one mounted Agent application."""

from __future__ import annotations
from XBotv2.permissions.api.models import PERMISSIONS

from XBotv2.agentloop.api.state import LOOP_STATE, LoopState
from XBotv2.usage.api.service import USAGE
from dataclasses import dataclass
from xcore.api.context import Context
from XBotv2.agentloop.api.contracts import AgentLoopDriverPort
from XBotv2.application.contracts import (
    ApplicationSnapshot,
    ClientEventsPort,
    COLLECT_STATUS_SLOTS,
    SessionHistoryPort,
    StatusSlots,
    UsageSnapshotPort,
)
from XBotv2.permissions.api.models import PermissionsPort
from XBotv2.core.api.artifacts import ArtifactStorePort
from XBotv2.core.api.history import HistoryReader


@dataclass(slots=True)
class MountedAgentApplication:
    """Expose host operations without leaking the XCore service container."""

    _context: Context
    events: Context
    driver: AgentLoopDriverPort
    artifacts: ArtifactStorePort
    client_events: ClientEventsPort
    history: SessionHistoryPort
    history_pages: HistoryReader
    usage: UsageSnapshotPort
    loop_state: LoopState
    parent_permissions: PermissionsPort
    persistence_available: bool

    async def status_slots(self) -> dict[str, str]:
        slots = StatusSlots()
        await self.events.emit(COLLECT_STATUS_SLOTS, slots)
        return dict(slots.values)

    async def snapshot(self) -> ApplicationSnapshot:
        return ApplicationSnapshot(
            messages=self.loop_state.history.snapshot(),
            usage=self.usage.snapshot(),
            metadata=self.loop_state.metadata.value,
            status_slots=await self.status_slots(),
        )

    async def close(self) -> None:
        await self._context.destroy()


async def mounted_application(context: Context) -> MountedAgentApplication:
    """Transfer a mounted Context to its public handle, or dispose on failure."""
    try:
        return MountedAgentApplication(
            _context=context,
            events=context,
            driver=context.require("engine"),
            artifacts=context.require("artifacts"),
            client_events=context.require("client_events"),
            history=context.require("session"),
            history_pages=(
                _TranscriptPages(context.thread_persistence.history)
                if context.has("thread_persistence")
                else _TranscriptPages(context.require(LOOP_STATE).history)
            ),
            usage=context.require(USAGE),
            loop_state=context.require(LOOP_STATE),
            parent_permissions=context.require(PERMISSIONS),
            persistence_available=context.has("thread_persistence"),
        )
    except BaseException as error:
        try:
            await context.destroy()
        except BaseException as cleanup_error:
            error.add_note(f"Application handoff cleanup failed: {cleanup_error!r}")
        raise


class _TranscriptPages:
    """Host projection adapter; model-surface reads remain persistence-internal."""

    def __init__(self, history: HistoryReader) -> None:
        self._history = history

    def page(self, *, limit: int, cursor: str | None = None):
        return self._history.page_transcript(limit=limit, cursor=cursor)


__all__ = ["MountedAgentApplication", "mounted_application"]
