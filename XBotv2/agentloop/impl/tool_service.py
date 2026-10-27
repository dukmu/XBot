"""Agent-loop service for tool registration and execution.

Creates the tool registry and registers it as ``ctx.tools``. Registration through the
services is a fiber effect: XCore tracks the currently applying fiber
(:func:`xcore.api.plugins.current_fiber`), so anything a plugin registers is undone
automatically when the plugin's fiber unloads — the service itself binds the
cleanup, no loader-side context tracking.

The execution pipeline lives on ``ToolsService``. Tool owners capture their
own invocation dependencies when registering a tool, and guard owners resolve
their own policy before returning a final decision. The service therefore has
no knowledge of individual plugins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import partial
from typing import Literal
from XBotv2.agentloop.api.contracts import (
    AllTools,
    ToolGuard,
    ToolRegistration,
    ToolSelection,
    ToolsPort,
)
from xcore.api.events import EventDispatch
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_runtime import execute_tools
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.core.api.tools import Tool, ToolCall, ToolExecution
from xcore.api.plugins import bound_effect, current_plugin_name
from xcore.api.context import Context


class ToolsService(ToolsPort):
    """Plugin-facing tool registry with fiber-scoped auto-unregister.

    Holds the tool registry plus the execution-pipeline guards.  A guard
    receives the ``ToolCall`` and its ``ToolEntry`` and returns ``None`` to
    let the call through, or a final :class:`GuardDecision`. Guards run in
    registration order and own any dependencies needed to reach that result.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        events: EventDispatch | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
        ownership: Literal["fiber", "caller"] = "fiber",
    ) -> None:
        self._registry = registry
        self.events = events
        self._log = runtime_log.bind("tools")
        # Composition-level default for registrations made outside a plugin
        # apply: "fiber" stays loud, "caller" says the composer owns release
        # (test harnesses, embedding hosts).
        self._ownership = ownership
        self._guards: list[tuple[object, ToolGuard, bool]] = []

    def guard(
        self,
        guard: ToolGuard,
        *,
        approval: bool = False,
        owner: Context | None = None,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> bool:
        """Register one monotonic execution guard.

        ``approval=True`` marks a guard that can approve sandbox-escape
        requests (an active approval layer). The execution pipeline fails
        closed: an escaped call without an approval-capable guard is denied
        before dispatch.

        Outside plugin ``apply``, pass the retained plugin context as ``owner``.
        Embedding callers can instead explicitly own cleanup.
        """
        token = object()
        remove = partial(self._remove_guard, token)
        if owner is not None:
            owner.effect(lambda: remove)
        elif bound_effect(remove) is False and (cleanup or self._ownership) != "caller":
            raise RuntimeError(
                "Tool guards must be registered from within a plugin "
                "apply() or with an owner; pass cleanup='caller' when the caller owns release"
            )
        self._guards.append((token, guard, approval))
        self._log.debug(
            "tool.guard.registered",
            owner=owner.fiber.name if owner is not None else current_plugin_name(),
            guard=getattr(guard, "__qualname__", type(guard).__qualname__),
            approval=approval,
        )
        return True

    def _remove_guard(self, token: object) -> None:
        self._guards[:] = [entry for entry in self._guards if entry[0] is not token]

    def guards(self) -> tuple[ToolGuard, ...]:
        return tuple(guard for _, guard, _ in self._guards)

    def approval_layer_active(self) -> bool:
        return any(approval for _, _, approval in self._guards)

    def register(
        self,
        tool: Tool,
        *,
        model_visible: bool = True,
        timeout_seconds: float | None = None,
        namespace: str | None = None,
        owner: Context | None = None,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> str:
        """Register one tool; undone automatically when the plugin unloads.

        ``namespace`` is only for functional name scoping (e.g. ``mcp:server``,
        ``skills:scope``); plugin ownership and cleanup are handled by the
        XCore fiber. Outside plugin ``apply``, pass the retained plugin context
        as ``owner``; embedding callers can explicitly own cleanup instead.
        """
        name = self._registry.register(
            tool,
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
            namespace=namespace,
        )
        entry = self._registry.get_registered(name)

        def remove() -> None:
            if self._registry.get_registered(name) is entry:
                self.unregister(name)

        try:
            if owner is not None:
                owner.effect(lambda: remove)
            elif bound_effect(remove) is False and (cleanup or self._ownership) != "caller":
                raise RuntimeError(
                    f"Tool {name!r} was registered outside a plugin apply(); "
                    "provide an owner or pass cleanup='caller' when the caller owns release"
                )
        except BaseException:
            remove()
            raise
        self._log.info(
            "tool.registered",
            name=name,
            owner=owner.fiber.name if owner is not None else current_plugin_name(),
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
        )
        return name

    def unregister(self, name: str) -> bool:
        removed = self._registry.unregister(name)
        if removed:
            self._log.info("tool.unregistered", name=name)
        return removed

    def enabled(self) -> tuple[Tool, ...]:
        return tuple(self._registry.get_all())

    def resolve(self, name: str, *, include_disabled: bool = False) -> Tool | None:
        entry = (
            self._registry.get_registered(name)
            if include_disabled
            else self._registry.get(name)
        )
        return entry.tool if entry is not None else None

    def names(self) -> tuple[str, ...]:
        return tuple(self._registry.names())

    def registered_names(self) -> tuple[str, ...]:
        return tuple(self._registry.registered_names())

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registry.registered_entries()

    def restrict(self, selection: ToolSelection) -> tuple[str, ...]:
        enabled = tuple(self._registry.restrict(selection))
        self._log.debug(
            "tool.selection.restricted",
            selectors=("*" if isinstance(selection, AllTools) else selection),
            enabled_count=len(enabled),
        )
        return enabled

    def exclude(self, selectors: list[str]) -> tuple[str, ...]:
        enabled = tuple(self._registry.exclude(selectors))
        self._log.debug(
            "tool.selection.excluded",
            selectors=selectors,
            enabled_count=len(enabled),
        )
        return enabled

    async def execute_all(
        self,
        tool_calls: list[ToolCall],
    ) -> list[ToolExecution]:
        """Run the full tool-execution guard pipeline.

        Pipeline per call: rewrite-only ``BEFORE_TOOL_CALL`` event, schema
        validation, monotonic guards, dispatch, and ``AFTER_TOOL_CALL``.
        Tool owners bind their runtime dependencies before registration; the
        agent loop only submits calls and receives their ordered results.
        """
        results: list[ToolExecution] = []
        async for message in self.execute_each(
            tool_calls,
        ):
            results.append(message)
        return results

    def execute_each(
        self,
        tool_calls: list[ToolCall],
    ) -> AsyncIterator[ToolExecution]:

        return execute_tools(
            tool_calls,
            self._registry,
            events=self.events,
            guards=self.guards(),
            runtime_log=self._log,
            approval_layer_active=self.approval_layer_active(),
        )
