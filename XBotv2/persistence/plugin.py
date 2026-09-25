"""Hydrate the thread persistence domains before Agent construction."""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context

from XBotv2.agentloop import AgentInbox, Events
from XBotv2.core.history import ConversationHistory
from XBotv2.core.metadata import (
    THREAD_METADATA_CHANGED,
    THREAD_METADATA_INITIALIZED,
    ThreadMetadataChanged,
    ThreadMetadataInitialized,
)
from XBotv2.core.paths import SessionPaths
from XBotv2.core.messages import HumanInputMessage, RuntimeNoticeMessage
from XBotv2.core.timing import conversation_stats
from XBotv2.persistence.store import (
    DeferredThreadMetadataStore,
    ThreadMetadataStore,
    ThreadPersistence,
)


def _materialize_after_first_turn(persistence: ThreadPersistence):
    """Flush deferred metadata once the first committed turn makes it durable."""

    async def _hook(_event: str, *_args: object) -> None:
        persistence.materialize()

    return _hook


def _save_metadata(store: ThreadMetadataStore | DeferredThreadMetadataStore):
    """Durable subscriber of the metadata fact: write every accepted change."""

    def _save(change: ThreadMetadataChanged | ThreadMetadataInitialized) -> None:
        store.save(change.current)

    return _save


def thread_persistence_factory(
    session_paths: SessionPaths,
    *,
    thread_id: str,
) -> ThreadPersistence:
    return ThreadPersistence.open(
        session_paths,
        thread_id=thread_id,
    )


class ThreadPersistenceComponent:
    inject = ["loop_state", "thread_persistence", "runtime_log"]
    name = "xbot.persistence"

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        state = ctx.loop_state
        persistence = ctx.thread_persistence
        messages = persistence.history.load_surface()
        committed_input_ids = {
            str(message.input_id)
            for message in messages
            if isinstance(message, HumanInputMessage)
        } | {
            str(message.notice_id)
            for message in messages
            if isinstance(message, RuntimeNoticeMessage)
        }
        pending_inputs = persistence.inbox.reconcile(committed_input_ids)
        state.set_history(ConversationHistory(messages, sink=persistence.history))
        # Restore the lifetime turn counter from the durable surface once:
        # compaction folds ``SessionStats`` (including turns) into the summary
        # message, so this value survives compaction and restart. History
        # mutations never recompute it.
        state.restore_turn_count(conversation_stats(messages).turns)
        if state.resumed is False and isinstance(
            persistence.metadata, DeferredThreadMetadataStore
        ):
            ctx.on(Events.TURN_END, _materialize_after_first_turn(persistence))
        state.restore_resumed(persistence.has_persisted_state())
        # The durable inbox is composed here and handed to the loop driver at
        # construction, so its availability, not plugin-tree order, decides
        # when the engine can be built.
        ctx.set("agent_inbox", AgentInbox(
            events=ctx,
            items=pending_inputs,
            sink=persistence.inbox,
        ))
        # Durability subscribes to the metadata fact, not to the value holder:
        # the state announces, persistence reacts. The initial load happens
        # before the listener is registered, so hydration never rewrites the
        # file it just read; every later change is saved by the listener.
        stored_metadata = persistence.metadata.load()
        if stored_metadata is not None:
            await state.metadata.initialize(stored_metadata)
        save_metadata = _save_metadata(persistence.metadata)
        ctx.on(THREAD_METADATA_CHANGED, save_metadata)
        ctx.on(THREAD_METADATA_INITIALIZED, save_metadata)

        ctx.runtime_log.bind("persistence").info(
            "persistence.hydrated",
            session_id=persistence.session_id,
            thread_id=persistence.thread_id,
            history_messages=len(messages),
            pending_inputs=len(pending_inputs),
            resumed=state.resumed,
        )


async def mount_thread_persistence(ctx: Context) -> None:
    await ThreadPersistenceComponent().apply(ctx)


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
