"""Fiber-effect helpers for plugin-facing capability services.

XCore tracks the fiber whose ``apply`` is currently executing
(:func:`xcore.current_fiber`).  Capability services (``ctx.tools`` /
``ctx.commands`` / ``ctx.prompts`` / ``ctx.agents``) read it to attach
fiber-scoped cleanup, so anything a plugin registers is undone automatically
when its fiber unloads — no loader-side context tracking.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from xcore import current_fiber

logger = logging.getLogger("xbot.effects")


def _active_fiber() -> Any:
    return current_fiber()


def _active_plugin_name() -> str:
    fiber = current_fiber()
    runtime = getattr(fiber, "runtime", None)
    if runtime is not None:
        return runtime.definition.name
    return "unknown"


def _effect_cleanup(fiber: Any, disposer: Callable[[], Any]) -> None:
    """Register a disposer on a fiber when one is active (never raises)."""
    if fiber is None:
        return
    try:
        fiber.effect(lambda: disposer)
    except Exception:  # noqa: BLE001 - cleanup registration must not break setup
        logger.exception("failed to register cleanup effect")


__all__ = ["_active_fiber", "_active_plugin_name", "_effect_cleanup"]
