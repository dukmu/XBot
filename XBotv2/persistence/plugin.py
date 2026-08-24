"""Hydrate the thread persistence domains before Agent construction."""

from __future__ import annotations

from xcore import Context

from XBotv2.core.history import ConversationHistory
from XBotv2.core.metadata import ThreadMetadataState


class PersistenceComponent:
    inject = ["loop_state", "thread_persistence"]
    name = "xbot.persistence"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        state = ctx.loop_state
        persistence = ctx.thread_persistence
        messages = persistence.history.load()
        committed_input_ids = {
            message.input_id for message in messages if message.input_id
        }
        pending_inputs = persistence.inbox.reconcile(committed_input_ids)
        state.set_history(ConversationHistory(messages, sink=persistence.history))
        state.resumed = persistence.has_persisted_state()
        state.metadata = ThreadMetadataState(
            persistence.metadata.load(),
            sink=persistence.metadata,
        )
        state.inbox_items = pending_inputs
        state.inbox_sink = persistence.inbox
        state.session.provider = persistence.provider

        ctx.set("thread_metadata", state.metadata)


plugin = PersistenceComponent()

__all__ = ["PersistenceComponent"]
