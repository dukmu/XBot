"""HookManager: central hook registry and executor."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from xbotv2.contracts import HookAction, HookDecision
from xbotv2.hooks.types import HookFn, HookStage, HookContext, SHORT_CIRCUIT_STAGES, STRICT_FAILURE_STAGES

logger = logging.getLogger("xbotv2.hooks")


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[HookStage, list[HookFn]] = defaultdict(list)

    def register(self, stage: HookStage, fn: HookFn) -> None:
        self._hooks[stage].append(fn)

    def unregister(self, stage: HookStage, fn: HookFn) -> bool:
        hooks = self._hooks.get(stage, [])
        for index in range(len(hooks) - 1, -1, -1):
            if hooks[index] is fn:
                del hooks[index]
                return True
        return False

    async def run(self, stage: HookStage, ctx: HookContext, *, short_circuit: bool | None = None) -> Any:
        if short_circuit is None:
            short_circuit = stage in SHORT_CIRCUIT_STAGES
        ctx.stage = stage
        errors: list[BaseException] = []
        strict_failure = stage in STRICT_FAILURE_STAGES and not short_circuit
        for hook in self._hooks.get(stage, []):
            try:
                result = await hook(ctx)
                if isinstance(result, HookDecision):
                    if result.action is HookAction.CONTINUE:
                        continue
                    if not short_circuit:
                        logger.warning(
                            "Ignoring control-flow decision from observer hook %r at %s",
                            hook,
                            stage.value,
                        )
                        continue
                    ctx.short_circuit_result = result
                    return result
                if short_circuit and result is not None:
                    ctx.short_circuit_result = result
                    return result
            except Exception as exc:
                if short_circuit:
                    raise
                if strict_failure:
                    errors.append(exc)
                logger.exception("Hook %r failed for stage %s", hook, stage.value)
        if errors:
            raise ExceptionGroup(f"Hook failures for stage {stage.value}", errors)
        return None
