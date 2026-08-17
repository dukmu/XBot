"""Interactions component: live client interaction coordination as a plugin.

Provides ``ctx.interactions`` — the in-memory coordination for model-facing
interaction requests (``ask_user`` input, permission requests).  Each live
engine turn owns an ``InteractionWaiter`` per interaction kind; the session
answers and cancels through the engine's waiters.
"""

from __future__ import annotations

from typing import Any

from XBotv2.interactions.interactions import (
    InteractionDisconnected,
    InteractionNotPending,
    InteractionResult,
    InteractionWaiter,
)


class InteractionsService:
    """Factory for per-engine interaction waiters."""

    def new_waiter(self) -> InteractionWaiter:
        return InteractionWaiter()


class InteractionsComponent:
    """Register the interactions factory as ``ctx.interactions``."""

    name = "xbot.interactions"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("interactions", InteractionsService())


plugin = InteractionsComponent()
