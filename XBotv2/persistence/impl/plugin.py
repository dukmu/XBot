"""Provide thread storage and subscribe to runtime persistence boundaries."""

from __future__ import annotations
from XBotv2.persistence.impl.client_http import build_router as build_client_router
from XBotv2.server.api.contracts import contribute_router

from XBotv2.agentloop.api.state import LOOP_STATE
from pydantic import JsonValue
from xcore.api.context import Context
from xcore.api.state import StateService
from XBotv2.agentloop.api.events import Events
from XBotv2.core.api.metadata import (
    THREAD_METADATA_CHANGED,
    THREAD_METADATA_INITIALIZED,
    ThreadMetadataChanged,
    ThreadMetadataInitialized,
)
from XBotv2.session.api.contracts import SESSION_INFO, THREAD_INFO
from XBotv2.persistence.api.contracts import PERSISTENCE, THREAD_PERSISTENCE
from XBotv2.persistence.impl.artifacts import ArtifactStore
from XBotv2.persistence.impl.session_lock import acquire_session
from XBotv2.persistence.impl.store import (
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


class ThreadPersistenceComponent:
    inject = [LOOP_STATE, "thread_persistence", "runtime_log"]
    name = "xbot.persistence"

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        state = ctx.require(LOOP_STATE)
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


from XBotv2.persistence.impl.archive import ArchiveReads


from XBotv2.persistence.impl.session_files import fork_persisted_session, delete_persisted_session

class PersistenceResources(ArchiveReads):
    def __init__(self, paths, runtime_log):
        self._paths = paths
        self._log = runtime_log

    def fork_session(self, session_id):
        return fork_persisted_session(self._paths, session_id)

    def delete_session(self, session_id):
        delete_persisted_session(self._paths, session_id)

    def thread(self, session_paths, *, thread_id):
        return ThreadPersistence.open(session_paths, thread_id=thread_id)

    def artifacts(self, thread_paths):
        return ArtifactStore(thread_paths, self._log)

    def acquire(self, session_paths):
        return acquire_session(session_paths.root, label=session_paths.session_id)


def mount_process(ctx, config):
    ctx.inject(["server", "sessions", PERSISTENCE], mount_client_http)
    ctx.provide(PERSISTENCE, PersistenceResources(ctx.runtime_paths, ctx.runtime_log))
    ctx.set("state", StateService(path=ctx.runtime_paths.data_dir / "state.json"))

mount_process.inject = ("runtime_log", "runtime_paths")
mount_process.provide = (PERSISTENCE, "state")


def mount_session(ctx, config):
    info = ctx.require(SESSION_INFO)
    claim = ctx.require(PERSISTENCE).acquire(ctx.runtime_paths.session(info.session_id))
    ctx.dispose(claim.release)

mount_session.inject = (PERSISTENCE, SESSION_INFO, "runtime_paths")
mount_session.provide = ()


def mount_thread(ctx, config):
    info = ctx.require(THREAD_INFO)
    paths = ctx.runtime_paths.session(info.session_id).thread(info.thread_id)
    store = ThreadPersistence.create(
        paths.session, thread_id=info.thread_id,
        state=StateService(path=paths.plugin_state_file),
        defer_metadata=not paths.metadata_file.exists(),
    )
    ctx.provide(THREAD_PERSISTENCE, store)
    ctx.set("artifacts", ctx.require(PERSISTENCE).artifacts(paths))
    ctx.inject(ThreadPersistenceComponent.inject, mount_thread_persistence)

mount_thread.inject = (PERSISTENCE, THREAD_INFO, "runtime_paths", "runtime_log")
mount_thread.provide = (THREAD_PERSISTENCE, "artifacts")


async def mount_client_http(ctx):
    await contribute_router(ctx, owner="xbot.persistence.client-http", router=build_client_router(sessions=ctx.sessions, persistence=ctx.require(PERSISTENCE)))
