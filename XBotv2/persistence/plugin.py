"""Provide thread storage and subscribe to runtime persistence boundaries."""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context, StateService

from XBotv2.agentloop import Events
from XBotv2.core.metadata import (
    THREAD_METADATA_CHANGED,
    THREAD_METADATA_INITIALIZED,
    ThreadMetadataChanged,
    ThreadMetadataInitialized,
)
from XBotv2.core.paths import SessionPaths
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
        ctx.dispose(persistence.materialize)
        if state.resumed is False and isinstance(
            persistence.metadata, DeferredThreadMetadataStore
        ):
            ctx.on(Events.TURN_END, _materialize_after_first_turn(persistence))
        # Durability subscribes to the metadata fact, not to the value holder:
        # the state announces, persistence reacts. The initial load happens
        # before the listener is registered, so hydration never rewrites the
        # file it just read; every later change is saved by the listener.
        save_metadata = _save_metadata(persistence.metadata)
        ctx.on(THREAD_METADATA_CHANGED, save_metadata)
        ctx.on(THREAD_METADATA_INITIALIZED, save_metadata)

        ctx.runtime_log.bind("persistence").info(
            "persistence.attached",
            session_id=persistence.session_id,
            thread_id=persistence.thread_id,
            history_messages=len(state.messages),
            resumed=state.resumed,
        )


async def mount_thread_persistence(ctx: Context) -> None:
    await ThreadPersistenceComponent().apply(ctx)


def mount_thread_store(ctx: Context) -> None:
    launch = ctx.session_launch
    ctx.set("thread_persistence", ThreadPersistence.create(
        launch.session_paths,
        thread_id=launch.thread_id,
        state=StateService(path=launch.session_paths.thread(launch.thread_id).plugin_state_file),
        defer_metadata=launch.defer_persist,
    ))


class PersistencePlugin:
    """Expose process readers, thread stores and durable event subscribers."""

    name = "xbot.persistence"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        ctx.set("thread_persistence_factory", thread_persistence_factory)
        ctx.inject(["session_launch"], mount_thread_store)
        ctx.inject(ThreadPersistenceComponent.inject, mount_thread_persistence)


plugin = PersistencePlugin()

__all__ = ["PersistencePlugin"]
