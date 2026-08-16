"""Persistence component: the session state store as an XCore service.

The store is created by the composition root (bootstrap) so assembly-time
reads (thread metadata, state paths) stay available before the plugin tree
mounts; this plugin owns the ``ctx.state_store`` service.  Backend selection
(jsonl today; sqlite and others later) is a plugin capability: swap the
backend in ``persistence.store.create_state_store`` without touching the
service contract.
"""

from __future__ import annotations

from typing import Any


class PersistenceComponent:
    """Register the session state store as ``ctx.state_store``."""

    name = "xbot.persistence"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("state_store", config["state_store"])


plugin = PersistenceComponent()
