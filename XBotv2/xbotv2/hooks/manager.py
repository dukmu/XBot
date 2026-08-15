"""HookManager: 41-stage hook bridge backed by an XCore event bus.

The engine-facing contract is unchanged: plugins and core code register
callbacks per :class:`HookStage` and the engine calls ``run(stage, ctx, ...)``.
What changed in the migration (see ``XCore/docs/05-migration-plan.md``):

- Listener storage lives on the XCore event bus (one bus per session runtime).
  A hook stage is a named event (``stage.value``); registration order, prepend
  support, and ownership-based auto-cleanup (plugin unload removes its hooks)
  come from the bus for free.
- ``run()`` gathers listeners via ``EventBus.hooks_for`` and applies the
  documented contract: observer/transform/guard result validation,
  short-circuit stages, and strict-failure aggregation.
- Plugin hooks receive a ``plugin_runtime`` on the ``HookContext`` so plugins
  (skills, MCP) can register dynamic tools/commands during hooks; the runtime
  context derives from the hook's owning plugin fiber.

Without a bus (``bus=None``) the manager keeps a local dictionary so isolated
contract tests keep working; production wiring always passes the bus.
"""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from typing import Any, Callable

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
        "messages", "tools", "llm", "compact_reason", "compact_metrics",
        "event", "turn_complete",
    }),
    HookStage.AFTER_AGENT: frozenset({"messages", "event", "turn_complete"}),
    HookStage.BEFORE_TOOL_CALL: frozenset({
        "tool_call", "args", "tool_result", "deny_reason",
    }),
    HookStage.AFTER_TOOLS: frozenset({"tool_results"}),
}


class HookManager:
    """Hook registry and executor, backed by an XCore event bus."""

    def __init__(
        self,
        bus: Any = None,
        *,
        plugin_runtime_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        """Create a hook manager.

        Args:
            bus: The XCore ``Context`` whose event bus stores listeners. When
                ``None``, listeners stay in a local per-stage dictionary
                (isolated use; plugin ownership is unavailable).
            plugin_runtime_factory: Resolver from a hook's owning context to
                the plugin runtime context injected on ``HookContext`` during
                ``run`` (owned by the migration bridge).
        """
        self._context = bus
        self._bus = getattr(bus, "_bus", None) if bus is not None else None
        self._local: dict[HookStage, list[HookFn]] = defaultdict(list)
        self._plugin_runtime_factory = plugin_runtime_factory

    # -- registration -------------------------------------------------------

    def register(self, stage: HookStage, fn: HookFn) -> None:
        """Register a hook callback (registration order preserved)."""
        if self._context is not None:
            self._context.on(stage.value, fn, global_=True)
        else:
            self._local[stage].append(fn)

    def unregister(self, stage: HookStage, fn: HookFn) -> bool:
        """Remove a hook callback by identity."""
        if self._context is not None:
            return self._context.off(stage.value, fn)
        hooks = self._local.get(stage, [])
        for index in range(len(hooks) - 1, -1, -1):
            if hooks[index] is fn:
                del hooks[index]
                return True
        return False

    def hook_count(self, stage: HookStage) -> int:
        """Number of registered hooks for a stage (diagnostics)."""
        if self._bus is not None:
            return self._bus.listener_count(stage.value)
        return len(self._local.get(stage, []))

    # -- execution ----------------------------------------------------------

    def _gather(self, stage: HookStage) -> list[tuple[HookFn, Any]]:
        """Return ``(callback, owner_ctx)`` pairs in registration order."""
        if self._bus is not None:
            return [
                (hook.callback, hook.owner)
                for hook in self._bus.hooks_for(stage.value)
            ]
        return [(fn, None) for fn in self._local.get(stage, [])]

    async def run(
        self,
        stage: HookStage,
        ctx: HookContext,
        *,
        short_circuit: bool | None = None,
    ) -> dict[str, Any] | HookDecision | None:
        if short_circuit is None:
            short_circuit = stage in SHORT_CIRCUIT_STAGES
        ctx.stage = stage
        ctx.short_circuit_result = None
        errors: list[BaseException] = []
        allowed_decision: HookDecision | None = None
        strict_failure = stage in STRICT_FAILURE_STAGES and not short_circuit
        for callback, owner in self._gather(stage):
            previous_runtime = getattr(ctx, "plugin_runtime", None)
            if self._plugin_runtime_factory is not None and owner is not None:
                ctx.plugin_runtime = self._plugin_runtime_factory(owner)
            try:
                result = callback(ctx)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                if short_circuit:
                    raise
                if strict_failure:
                    errors.append(exc)
                logger.exception("Hook %r failed for stage %s", callback, stage.value)
                continue
            finally:
                ctx.plugin_runtime = previous_runtime
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
