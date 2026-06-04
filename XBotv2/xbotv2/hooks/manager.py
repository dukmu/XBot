"""HookManager: central hook registry and executor."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from xbotv2.hooks.types import HookFn, HookStage, HookContext, SHORT_CIRCUIT_STAGES

logger = logging.getLogger("xbotv2.hooks")

# Union of all recognised stage strings (for robust input validation)
_STAGE_STRINGS: set[str] = {s.value for s in HookStage}


class HookManager:
    """Central hook registry and executor.

    Hooks run in registration order. For loop hooks (before/after context,
    agent, tools), the first truthy return value short-circuits the stage.

    For lifecycle hooks (session, turn, message, error, config), all
    registered callbacks always run; errors are logged and do not prevent
    other callbacks from executing.

    Usage::

        manager = HookManager()
        manager.register(HookStage.BEFORE_AGENT, my_hook)
        result = await manager.run(HookStage.BEFORE_AGENT, ctx)
    """

    def __init__(self) -> None:
        self._hooks: dict[HookStage, list[HookFn]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, stage: HookStage | str, fn: HookFn) -> None:
        """Register *fn* for *stage*."""
        stage = self._normalize_stage(stage)
        self._hooks[stage].append(fn)

    def register_many(self, hooks: list[tuple[HookStage | str, HookFn]]) -> None:
        """Batch-register (stage, fn) pairs."""
        for stage, fn in hooks:
            self.register(stage, fn)

    def clear(self, stage: HookStage | str | None = None) -> None:
        """Remove all hooks, or all hooks for *stage*."""
        if stage is not None:
            self._hooks[self._normalize_stage(stage)].clear()
        else:
            self._hooks.clear()

    def unregister(self, stage: HookStage | str, fn: HookFn) -> bool:
        """Remove one registered hook function from *stage*.

        Returns ``True`` if a hook was removed.
        """
        stage = self._normalize_stage(stage)
        hooks = self._hooks.get(stage, [])
        for index in range(len(hooks) - 1, -1, -1):
            if hooks[index] is fn:
                del hooks[index]
                return True
        return False

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run(
        self, stage: HookStage | str, ctx: HookContext, *, short_circuit: bool | None = None
    ) -> Any:
        """Execute all hooks for *stage*.

        If *short_circuit* is ``True`` (the default for loop hooks), the
        first truthy return value stops execution and is returned.

        For lifecycle hooks, *short_circuit* defaults to ``False`` so all
        callbacks run regardless of individual return values.
        """
        stage = self._normalize_stage(stage)

        if short_circuit is None:
            short_circuit = stage in SHORT_CIRCUIT_STAGES

        ctx.stage = stage

        for hook in self._hooks.get(stage, []):
            try:
                result = await hook(ctx)
                if short_circuit and result is not None:
                    ctx.short_circuit_result = result
                    return result
            except Exception:
                if short_circuit:
                    raise
                logger.exception("Hook %r failed for stage %s — continuing", hook, stage.value)

        return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def count(self, stage: HookStage | str | None = None) -> int:
        """Return the number of registered hooks."""
        if stage is not None:
            return len(self._hooks.get(self._normalize_stage(stage), []))
        return sum(len(v) for v in self._hooks.values())

    def stages(self) -> list[HookStage]:
        """Return stages that have at least one hook registered."""
        return [s for s, fns in self._hooks.items() if fns]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_stage(stage: HookStage | str) -> HookStage:
        if isinstance(stage, HookStage):
            return stage
        if stage in _STAGE_STRINGS:
            return HookStage(stage)
        raise ValueError(f"Unknown hook stage: {stage!r}")
