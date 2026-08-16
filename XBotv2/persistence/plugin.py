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


plugin = PersistenceComponent()
