"""Host-level persistence read service for the server composition root.

Persistence is mounted per-session in the agent tree (``persistence/plugin.py``
requires ``loop_state`` and ``thread_paths``). This host entry exposes only the
on-disk reader factory so the session host (server tree) can read inactive
thread summaries, policy, and metadata without importing ``persistence``
internals directly.
"""

from __future__ import annotations

from typing import Any


def state_store_factory(
    session_paths: Any,
    *,
    thread_id: str = "",
    workspace_root: str = "",
    provider: str = "",
) -> Any:
    """Construct a :class:`CoreStateStore` reader for a persisted thread.

    ``session_paths`` is a ``SessionPaths`` object from
    ``RuntimePaths.session(session_id)``.
    """
    from XBotv2.persistence.store import CoreStateStore

    return CoreStateStore(
        session_paths,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider=provider,
    )


class PersistenceHost:
    """Provides ``ctx.state_store_factory`` in the server composition root."""

    name = "xbot.persistence.host"

    def apply(self, ctx, config=None) -> None:
        ctx.set("state_store_factory", state_store_factory)


plugin = PersistenceHost()