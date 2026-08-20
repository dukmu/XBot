"""Persistence component: the session state store as an XCore service.

This plugin owns ``ctx.state_store`` and projects the session-owned
``ctx.loop_state`` to storage. It hydrates that state during activation rather
than constructing the state consumed by the loop.
"""

from __future__ import annotations

from typing import Any

from XBotv2.agentloop import Events, LoopState
from XBotv2.session import PREPARE_FORK


class PersistenceService:
    """Synchronize one core loop-state projection to its storage backend."""

    def __init__(self, store: Any, state: LoopState) -> None:
        self.store = store
        self.state = state
        self._refs = list(state.messages)
        self._fingerprints = [message.fingerprint() for message in state.messages]

    async def state_changed(self, event: Any) -> bool:
        details = event.event if isinstance(event.event, dict) else {}
        operation = details.get("history_operation")
        return self._sync(operation)

    async def flush(self) -> bool:
        """Explicit application-level durability barrier."""
        return self._sync(None)

    def _sync(self, operation: tuple[str, int] | None) -> bool:
        current = self.state.messages
        unchanged = (
            operation is None
            and len(current) == len(self._refs)
            and len(self._fingerprints) == len(self._refs)
            and all(a is b for a, b in zip(current, self._refs))
            and all(
                fingerprint == message.fingerprint()
                for fingerprint, message in zip(self._fingerprints, current)
            )
        )
        if unchanged:
            return False
        if operation is None:
            self.store.sync_messages(current)
        else:
            name, turns = operation
            if name == "clear":
                self.store.append_clear()
            elif name == "undo":
                self.store.append_undo(turns)
            else:
                self.store.append_checkpoint(current, reason=name)
        self._refs = list(current)
        self._fingerprints = [message.fingerprint() for message in current]
        return True


class PersistenceComponent:
    """Hydrate and persist the session-owned core loop state."""

    inject = ["loop_state", "thread_paths", "session_launch"]

    name = "xbot.persistence"

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.persistence.store import CoreStateStore

        config = config or {}
        state = ctx.loop_state
        store = CoreStateStore.create(
            ctx.thread_paths,
            thread_id=state.session.thread_id,
            workspace_root=state.session.workspace_root,
            provider=ctx.session_launch.provider_name,
        )
        ctx.set("state_store", store)
        resumed = store.has_existing_session()
        messages = store.read_messages() if resumed else []
        state.messages = messages
        state.turn_count = sum(1 for message in messages if message.role == "user")
        state.resumed = resumed
        state.metadata = store.read_thread_metadata()
        state.inbox_events = store.read_events(Events.INBOX_SPLICE)
        state.media_root = str(store.root)
        state.session.provider = store.provider
        state.session.turn_count = state.turn_count
        service = PersistenceService(store, state)
        ctx.set("persistence", service)
        ctx.on(Events.STATE_CHANGED, service.state_changed)
        ctx.on(PREPARE_FORK, lambda _request: service.flush())

        async def persist_session_metadata(event: Any) -> None:
            store.provider = state.session.provider
            store.write_thread_metadata(state.metadata)

        ctx.on(Events.SESSION_INIT, persist_session_metadata)
        ctx.on(Events.AGENT_CONFIGURED, persist_session_metadata)

        async def persist_runtime_event(event: Any) -> None:
            record = event.client_event
            if record is not None:
                store.append_event(record.type, record.data)

        ctx.on(Events.INBOX_SPLICE, persist_runtime_event)


plugin = PersistenceComponent()

__all__ = ["PersistenceComponent", "PersistenceService"]
