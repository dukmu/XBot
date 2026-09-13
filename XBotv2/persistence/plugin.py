"""Hydrate the thread persistence domains before Agent construction."""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context

from XBotv2.agentloop import Events
from XBotv2.core.history import ConversationHistory
from XBotv2.persistence.store import DeferredThreadMetadataStore, ThreadPersistence
from XBotv2.core.metadata import ThreadMetadataState
from XBotv2.core.paths import SessionPaths
from XBotv2.persistence.store import ThreadPersistence


def _materialize_after_first_turn(persistence: ThreadPersistence):
    """Flush deferred metadata once the first committed turn makes it durable."""

    async def _hook(_event: str, *_args: object) -> None:
        persistence.materialize()

    return _hook


def thread_persistence_factory(
    session_paths: SessionPaths,
    *,
    thread_id: str = "",
    workspace_root: str = "",
    provider: str = "",
) -> ThreadPersistence:
    return ThreadPersistence.open(
        session_paths,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider=provider,
    )


class ThreadPersistenceComponent:
    inject = ["loop_state", "thread_persistence", "runtime_log"]
    name = "xbot.persistence"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        state = ctx.loop_state
        persistence = ctx.thread_persistence
        nodes = persistence.history.load_surface()
        messages = [node.message for node in nodes]
        committed_input_ids = {
            message.input_id for message in messages if message.input_id
        }
        pending_inputs = persistence.inbox.reconcile(committed_input_ids)
        state.set_history(ConversationHistory(sink=persistence.history, nodes=nodes))
        if state.resumed is False and isinstance(
            persistence.metadata, DeferredThreadMetadataStore
        ):
            ctx.on(Events.TURN_END, _materialize_after_first_turn(persistence))
        state.resumed = persistence.has_persisted_state()
        state.metadata = ThreadMetadataState(
            persistence.metadata.load(),
            sink=persistence.metadata,
        )
        state.inbox_items = pending_inputs
        state.inbox_sink = persistence.inbox
        state.session.provider = persistence.provider

        ctx.runtime_log.bind("persistence").info(
            "persistence.hydrated",
            session_id=persistence.session_id,
            thread_id=persistence.thread_id,
            history_messages=len(messages),
            pending_inputs=len(pending_inputs),
            resumed=state.resumed,
            provider=persistence.provider,
        )

        ctx.set("thread_metadata", state.metadata)


def mount_thread_persistence(ctx: Context) -> None:
    ThreadPersistenceComponent().apply(ctx)


class PersistencePlugin:
    """Expose process readers and hydrate thread-local persistent state."""

    name = "xbot.persistence"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        ctx.set("thread_persistence_factory", thread_persistence_factory)
        ctx.inject(ThreadPersistenceComponent.inject, mount_thread_persistence)


plugin = PersistencePlugin()

__all__ = ["PersistencePlugin"]
