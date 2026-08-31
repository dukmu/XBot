"""Host-level persistence read service for the server composition root.

Persistence is mounted per-session in the agent tree (``persistence/plugin.py``
requires ``loop_state`` and ``thread_paths``). This host entry exposes only the
on-disk reader factory so the session host (server tree) can read inactive
thread summaries, policy, and metadata without importing ``persistence``
internals directly.
"""

from __future__ import annotations

from xcore import Context

from XBotv2.core.paths import SessionPaths
from XBotv2.persistence.store import ThreadPersistence


def thread_persistence_factory(
    session_paths: SessionPaths,
    *,
    thread_id: str = "",
    workspace_root: str = "",
    provider: str = "",
) -> ThreadPersistence:
    """Construct a :class:`ThreadPersistence` reader for a persisted thread.

    ``session_paths`` is a ``SessionPaths`` object from
    ``RuntimePaths.session(session_id)``.
    """
    return ThreadPersistence.open(
        session_paths,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider=provider,
    )


class PersistenceHost:
    """Provide inactive-thread persistence readers to session management."""

    name = "xbot.persistence.host"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.set("thread_persistence_factory", thread_persistence_factory)
