"""Persistence component: the session state store as an XCore service.

This plugin owns ``ctx.state_store`` and creates the store itself from its
tree config (session paths + thread identity + provider) — no composition-root
pre-initialization.  Backend selection (jsonl today; sqlite and others later)
is a plugin capability: swap the backend in
``persistence.store.create_state_store`` without touching the service
contract.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.events import Events
from XBotv2.core.loop import LoopState
from XBotv2.core.runtime import SessionInfo


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
    """Create the session state store and register it as ``ctx.state_store``."""

    name = "xbot.persistence"

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.persistence.store import CoreStateStore

        config = config or {}
        store = CoreStateStore.create(
            config["session_paths"],
            thread_id=config["thread_id"],
            workspace_root=config["workspace_root"],
            provider=config["provider"],
        )
        ctx.set("state_store", store)
        resumed = store.has_existing_session()
        messages = store.read_messages() if resumed else []
        state = LoopState(
            session=SessionInfo(
                session_id=store.session_id,
                thread_id=store.thread_id,
                workspace_root=str(config["workspace_root"]),
                provider=str(config["provider"]),
            ),
            messages=messages,
            turn_count=sum(1 for message in messages if message.role == "user"),
            resumed=resumed,
            metadata=store.read_thread_metadata(),
            inbox_events=store.read_events(Events.INBOX_SPLICE),
            media_root=str(store.root),
        )
        state.session.turn_count = state.turn_count
        ctx.set("loop_state", state)
        service = PersistenceService(store, state)
        ctx.set("persistence", service)
        ctx.on(Events.STATE_CHANGED, service.state_changed)

        async def persist_session_metadata(event: Any) -> None:
            store.provider = state.session.provider
            store.write_thread_metadata(state.metadata)

        ctx.on(Events.SESSION_INIT, persist_session_metadata)

        async def persist_runtime_event(event: Any) -> None:
            record = event.client_event or {}
            store.append_event(
                str(record.get("type") or ""),
                dict(record.get("data") or {}),
            )

        ctx.on(Events.INBOX_SPLICE, persist_runtime_event)


plugin = PersistenceComponent()

__all__ = ["PersistenceComponent", "PersistenceService"]
