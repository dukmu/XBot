"""HookManager: central hook registry and executor."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from xbotv2.api.hooks import (
    HookAction,
    HookContext,
    HookDecision,
    HookFn,
    HookStage,
    SHORT_CIRCUIT_STAGES,
    STRICT_FAILURE_STAGES,
)

logger = logging.getLogger("xbotv2.hooks")

_GUARD_STAGES = frozenset({
    HookStage.BEFORE_AGENT,
    HookStage.BEFORE_TOOLS,
    HookStage.BEFORE_TOOL_CALL,
})

_RESULT_KEYS: dict[HookStage, frozenset[str]] = {
    HookStage.BEFORE_USER_MESSAGE_ACCEPT: frozenset({
        "user_input", "event", "turn_complete",
    }),
    HookStage.BEFORE_CONTEXT: frozenset({
        "messages", "compact_reason", "compact_metrics",
    }),
    HookStage.PRE_COMPACT: frozenset({"messages", "compact_reason"}),
    HookStage.BEFORE_CONTEXT_BUILD: frozenset({
        "messages", "context_kwargs", "event", "turn_complete",
    }),
    HookStage.AFTER_CONTEXT: frozenset({
        "context_messages", "messages", "event", "turn_complete",
    }),
    HookStage.BEFORE_AGENT: frozenset({"messages"}),
    HookStage.BEFORE_TOOL_SCHEMA_BIND: frozenset({
        "tools", "messages", "event", "turn_complete",
    }),
    HookStage.BEFORE_MODEL_REQUEST: frozenset({
        "messages", "tools", "llm", "event", "turn_complete",
    }),
    HookStage.AFTER_AGENT: frozenset({"messages", "event", "turn_complete"}),
    HookStage.BEFORE_TOOL_CALL: frozenset({
        "tool_call", "args", "tool_result", "deny_reason",
    }),
    HookStage.AFTER_TOOLS: frozenset({"tool_results"}),
}


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
        ctx.short_circuit_result = None
        errors: list[BaseException] = []
        allowed_decision: HookDecision | None = None
        strict_failure = stage in STRICT_FAILURE_STAGES and not short_circuit
        for hook in self._hooks.get(stage, []):
            try:
                result = await hook(ctx)
            except Exception as exc:
                if short_circuit:
                    raise
                if strict_failure:
                    errors.append(exc)
                logger.exception("Hook %r failed for stage %s", hook, stage.value)
                continue
            self._validate_result(stage, result, short_circuit=short_circuit)
            if isinstance(result, HookDecision):
                if result.action is HookAction.CONTINUE:
                    continue
                if result.action is HookAction.ALLOW:
                    allowed_decision = result
                    continue
                ctx.short_circuit_result = result
                return result
            if result is not None:
                ctx.short_circuit_result = result
                return result
        if errors:
            raise ExceptionGroup(f"Hook failures for stage {stage.value}", errors)
        if allowed_decision is not None:
            ctx.short_circuit_result = allowed_decision
            return allowed_decision
        return None

    @staticmethod
    def _validate_result(
        stage: HookStage,
        result: Any,
        *,
        short_circuit: bool,
    ) -> None:
        if result is None:
            return
        if not short_circuit:
            raise TypeError(
                f"Observer hook {stage.value} must return None, got "
                f"{type(result).__name__}"
            )
        if isinstance(result, HookDecision):
            if stage not in _GUARD_STAGES:
                raise TypeError(
                    f"HookDecision is not valid at {stage.value}"
                )
            if (
                result.action is HookAction.ALLOW
                and stage is not HookStage.BEFORE_TOOL_CALL
            ):
                raise TypeError(
                    f"{result.action.value} is only valid at before_tool_call"
                )
            return
        if not isinstance(result, dict):
            raise TypeError(
                f"Short-circuit hook {stage.value} must return a dict or "
                f"HookDecision, got {type(result).__name__}"
            )
        allowed = _RESULT_KEYS.get(stage, frozenset())
        unknown = set(result) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"Hook {stage.value} returned unsupported keys: {names}")
        if not result:
            raise TypeError(f"Hook {stage.value} must not return an empty dict")
