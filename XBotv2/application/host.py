"""Narrow handle for one mounted Agent application."""

from __future__ import annotations

from dataclasses import dataclass

from xcore import Context

from XBotv2.agentloop import AgentLoopDriverPort
from XBotv2.application.services import (
    AgentApplicationSnapshot,
    ApplicationEventsPort,
    ClientEventsPort,
    COLLECT_STATUS_SLOTS,
    LoopStateView,
    SessionHistoryPort,
    StatusSlots,
    UsageSnapshotPort,
)
from XBotv2.permissions import PermissionsPort
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.history import ConversationPageReader


@dataclass(slots=True)
class MountedAgentApplication:
    """Expose host operations without leaking the XCore service container."""

    _context: Context
    events: ApplicationEventsPort
    driver: AgentLoopDriverPort
    artifacts: ArtifactStorePort
    client_events: ClientEventsPort
    history: SessionHistoryPort
    history_pages: ConversationPageReader
    usage: UsageSnapshotPort
    loop_state: LoopStateView
    parent_permissions: PermissionsPort
    persistence_available: bool

    async def status_slots(self) -> dict[str, str]:
        slots = StatusSlots()
        await self.events.emit(COLLECT_STATUS_SLOTS, slots)
        return dict(slots.values)

    async def snapshot(self) -> AgentApplicationSnapshot:
        settings = self.driver.settings
        return AgentApplicationSnapshot(
            agent=settings.agent_name,
            provider=settings.provider,
            model=settings.model,
            model_mode=settings.model_mode,
            context_window=self.driver.context_window,
            messages=tuple(self.driver.messages),
            usage=dict(self.usage.snapshot()),
            metadata=self.loop_state.metadata.value,
            status_slots=await self.status_slots(),
        )

    async def close(self) -> None:
        await self._context.destroy()


def mounted_application(context: Context) -> MountedAgentApplication:
    """Project a fully initialized XCore context to its host contract."""
    return MountedAgentApplication(
        _context=context,
        events=context,
        driver=context.engine,
        artifacts=context.artifacts,
        client_events=context.client_events,
        history=context.session,
        history_pages=(
            _TranscriptPages(context.thread_persistence.history)
            if context.has("thread_persistence")
            else _TranscriptPages(context.loop_state.history)
        ),
        usage=context.usage,
        loop_state=context.loop_state,
        parent_permissions=context.permissions,
        persistence_available=context.has("thread_persistence"),
    )


class _TranscriptPages:
    """Host projection adapter; model-surface reads remain persistence-internal."""

    def __init__(self, history: object) -> None:
        self._history = history

    def page(self, *, limit: int, cursor: str | None = None):
        return self._history.page_transcript(limit=limit, cursor=cursor)


__all__ = ["MountedAgentApplication", "mounted_application"]
