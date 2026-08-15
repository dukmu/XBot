"""HookManager: the 41-stage hook contract expressed on XCore public APIs.

The engine-facing contract is unchanged: plugins and core code register
callbacks per :class:`HookStage` and the engine calls ``run(stage, ctx, ...)``.
What changed in the migration (see ``XCore/docs/05-migration-plan.md``):

- Hook stages are ordinary XCore events (``stage.value``).  Plugins register
  with ``ctx.on(stage.value, callback)`` exactly like any other event;
  registration order, prepend, and ownership-based auto-cleanup (plugin unload
  removes its hooks) come from XCore for free.
- The hook *contract* (observer/transform/guard result validation,
  short-circuit, strict-failure aggregation, ``plugin_runtime`` injection) is
  enforced by a **per-listener wrapper** installed through XCore's public
  ``internal/listener`` registration interception -- no access to event bus
  internals.
- ``run()`` dispatches through the public primitives: ``emit`` for observer
  stages (all listeners run, strict failures aggregate on the HookContext),
  ``serial`` for short-circuit stages (first bail value wins, Cordis bail
  semantics).

This manager always has an XCore context: the one injected by bootstrap, or a
private one created on demand so standalone use (tests, embeddings) exercises
the exact same code path.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from xcore import Context

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

_HOOK_STAGE_NAMES = frozenset(stage.value for stage in HookStage)

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
    """41-stage hook registry and executor on an XCore context."""

    def __init__(
        self,
        bus: Context | None = None,
        *,
        plugin_runtime_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        """Create a hook manager.

        Args:
            bus: The XCore ``Context`` whose event bus stores hook listeners.
                ``None`` creates a private context so standalone use runs the
                same code path (no plugin ownership).
            plugin_runtime_factory: Resolver from a hook's registering context
                to the plugin runtime context injected on ``HookContext``
                (owned by the migration bridge).
        """
        self._context = bus if bus is not None else Context()
        self._plugin_runtime_factory = plugin_runtime_factory
        # One disposer per registration (the same fn may register twice).
        self._wrappers: dict[tuple[HookStage, Any], list[Callable[[], bool]]] = {}
        # Stage -> wrapped listeners this manager knows about (introspection).
        self._registry: dict[str, list[Any]] = {}
        self._install_hook_interception()

    # -- registration -------------------------------------------------------

    def _install_hook_interception(self) -> None:
        """Wrap hook-stage registrations with the contract wrapper.

        Uses XCore's public ``internal/listener`` interception: for hook-stage
        events the handler returns a re-registration of the wrapped listener
        (a bail result replaces the original registration).  The re-entry is
        recognized via a marker attribute, so wrapping is not recursive.
        """

        def handle(
            ctx: Any, name: str, listener: Any, options: dict[str, Any]
        ) -> Any:
            if name not in _HOOK_STAGE_NAMES:
                return None
            if getattr(listener, "__hook_contract__", None) is not None:
                return None
            stage = HookStage(name)
            wrapped = self._make_contract_wrapper(stage, listener, ctx)
            wrapped.__hook_contract__ = True
            wrapped.__wrapped__ = listener
            disposer = ctx.on(
                name,
                wrapped,
                global_=True,
                prepend=bool(options.get("prepend", False)),
            )
            self._registry.setdefault(name, []).append(wrapped)

            def dispose() -> bool:
                registered = self._registry.get(name, [])
                if wrapped in registered:
                    registered.remove(wrapped)
                    if not registered:
                        self._registry.pop(name, None)
                return disposer()

            return dispose

        self._context.on("internal/listener", handle, global_=True)

    def listeners(self, stage: HookStage) -> tuple[HookFn, ...]:
        """Raw hook callbacks for a stage, in registration order.

        XBot-side introspection (backed by this manager's own registry); the
        event bus remains encapsulated inside XCore.
        """
        return tuple(
            wrapped.__wrapped__ for wrapped in self._registry.get(stage.value, [])
        )

    def register(self, stage: HookStage, fn: HookFn) -> None:
        """Register a hook callback (registration order preserved)."""
        disposer = self._context.on(stage.value, fn, global_=True)
        self._wrappers.setdefault((stage, fn), []).append(disposer)

    def unregister(self, stage: HookStage, fn: HookFn) -> bool:
        """Remove one registration of a hook callback by identity."""
        disposers = self._wrappers.get((stage, fn))
        if disposers:
            disposer = disposers.pop()
            if not disposers:
                del self._wrappers[(stage, fn)]
            return disposer()
        return self._context.off(stage.value, fn)

    def hook_count(self, stage: HookStage) -> int:
        """Number of registered hooks for a stage (diagnostics)."""
        return self._context._bus.listener_count(stage.value)

    # -- contract wrapper ---------------------------------------------------

    def _make_contract_wrapper(
        self, stage: HookStage, listener: HookFn, owner: Any
    ) -> Callable[[HookContext], Any]:
        """Enforce the hook contract for one listener invocation.

        The wrapper is what the bus dispatches: it injects ``plugin_runtime``
        for the owning plugin, validates the listener's return value against
        the stage contract, applies short-circuit/guard semantics, and
        implements the strict-failure policy (aggregate on the HookContext
        collector for strict stages, log-and-continue otherwise).
        """

        async def wrapper(hook_ctx: HookContext) -> Any:
            collector = getattr(hook_ctx, "_hook_collector", None)
            short_circuit = getattr(
                hook_ctx, "_hook_short_circuit", stage in SHORT_CIRCUIT_STAGES
            )
            strict = stage in STRICT_FAILURE_STAGES and not short_circuit
            previous_runtime = getattr(hook_ctx, "plugin_runtime", None)
            if self._plugin_runtime_factory is not None:
                hook_ctx.plugin_runtime = self._plugin_runtime_factory(owner)
            try:
                result = listener(hook_ctx)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 - policy per stage
                if short_circuit:
                    raise
                if strict and collector is not None:
                    collector["errors"].append(exc)
                    return None
                logger.exception(
                    "Hook %r failed for stage %s", listener, stage.value
                )
                return None
            finally:
                hook_ctx.plugin_runtime = previous_runtime
            if result is None:
                return None
            self._validate_result(stage, result, short_circuit=short_circuit)
            if isinstance(result, HookDecision):
                if result.action in (HookAction.CONTINUE, HookAction.ALLOW):
                    if (
                        result.action is HookAction.ALLOW
                        and collector is not None
                    ):
                        collector["allow"] = result
                    return None
                # DENY / STOP: bail the serial dispatch.
                return result
            return result

        return wrapper

    # -- execution ----------------------------------------------------------

    async def run(
        self,
        stage: HookStage,
        ctx: HookContext,
        *,
        short_circuit: bool | None = None,
    ) -> dict[str, Any] | HookDecision | None:
        """Run all hooks for a stage under the documented contract."""
        if short_circuit is None:
            short_circuit = stage in SHORT_CIRCUIT_STAGES
        ctx.stage = stage
        ctx.short_circuit_result = None
        collector: dict[str, Any] = {"errors": [], "allow": None}
        ctx._hook_collector = collector
        ctx._hook_short_circuit = short_circuit
        if short_circuit:
            result = await self._context.serial(stage.value, ctx)
        else:
            await self._context.emit(stage.value, ctx)
            result = None
        if collector["errors"]:
            raise ExceptionGroup(
                f"Hook failures for stage {stage.value}", collector["errors"]
            )
        if result is None and collector["allow"] is not None:
            result = collector["allow"]
        ctx.short_circuit_result = result
        return result

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
