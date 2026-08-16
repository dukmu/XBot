"""Tools component: self-contained tool layer as XCore services.

Creates the tool-layer objects itself (ToolRegistry, ContextBuilder,
AgentRegistry, SandboxPolicy, PermissionSystem, JobRegistry) and registers
them as services, so the whole tool layer is one component in the plugin
tree.  Requires the runtime component (``ctx.runtime`` / ``ctx.variables`` /
``ctx.state_store``) and is mounted after it.

The component also owns the plugin-facing capability services
(:class:`ToolsService` / :class:`CommandsService` / :class:`PromptsService` /
:class:`AgentsService`).  Registration through these services is a fiber
effect: anything a plugin registers is undone automatically when the plugin
fiber unloads (replacing the loader's manual rollback tables).  Caller
tracking uses the :data:`_active_ctx` contextvar, which the loader sets for
the duration of ``apply`` so service methods know which fiber owns a
registration.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Callable

from xbotv2.api.commands import Command
from xbotv2.api.plugins import ToolRegistrationOptions
from xbotv2.api.tools import Tool
from xbotv2.core.agents import AgentRegistry
from xbotv2.core.context import ContextBuilder
from xbotv2.tools.permissions import PermissionIntersection, PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy
from xbotv2.api.jobs import JobKind, JobRegistry

logger = logging.getLogger("xbotv2.plugins.tools")

#: The plugin context currently executing (apply body or a plugin hook).
_active_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "xbotv2_plugin_ctx", default=None
)


def _with_plugin_ctx(ctx: Any, fn: Callable[[], Any]) -> Any:
    token = _active_ctx.set(ctx)
    try:
        return fn()
    finally:
        _active_ctx.reset(token)


def _active_fiber() -> Any:
    ctx = _active_ctx.get()
    return getattr(ctx, "fiber", None) if ctx is not None else None


def _active_plugin_name() -> str:
    ctx = _active_ctx.get()
    fiber = getattr(ctx, "fiber", None) if ctx is not None else None
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


# ---------------------------------------------------------------------------
# Capability services (registered on the XCore root context)
# ---------------------------------------------------------------------------


class ToolsService:
    """Plugin-facing tool registry with fiber-scoped auto-unregister."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def register(
        self,
        tool: Tool,
        options: ToolRegistrationOptions | None = None,
    ) -> str:
        registration = options or ToolRegistrationOptions()
        name = self.registry.register(
            tool,
            sandbox_mode=registration.sandbox_mode,
            namespace=registration.namespace,
            model_visible=registration.model_visible,
            timeout_seconds=registration.timeout_seconds,
        )
        _effect_cleanup(_active_fiber(), lambda: self.registry.unregister(name))
        return name

    def unregister(self, name: str) -> bool:
        return self.registry.unregister(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class CommandsService:
    """Plugin-facing command registry with fiber-scoped auto-unregister."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> str:
        if command.name in self._commands:
            raise ValueError(f"Command {command.name!r} is already registered")
        self._commands[command.name] = command
        _effect_cleanup(_active_fiber(), lambda: self._commands.pop(command.name, None))
        return command.name

    def unregister(self, name: str) -> bool:
        return self._commands.pop(name, None) is not None

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> tuple[Command, ...]:
        return tuple(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)


class PromptsService:
    """Plugin-facing prompt-fragment registry (per-plugin namespace)."""

    def __init__(self, context_builder: Any) -> None:
        self._builder = context_builder

    def add(
        self,
        stage: Any,
        text: str,
        *,
        source: str | None = None,
    ) -> None:
        plugin_name = _active_plugin_name()
        self._builder.register_fragment(stage, plugin_name, text, source=source)
        _effect_cleanup(
            _active_fiber(),
            lambda: self._builder.unregister_fragment(stage, plugin_name),
        )

    def remove(self, stage: Any, plugin_name: str) -> None:
        self._builder.unregister_fragment(stage, plugin_name)


class AgentsService:
    """Plugin-facing agent registry with fiber-scoped auto-unregister."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def register(self, definition: Any) -> str:
        owner = _active_plugin_name()
        name = self.registry.register(definition, owner=owner)
        _effect_cleanup(
            _active_fiber(),
            lambda: self.registry.unregister(name, owner=owner),
        )
        return name

    def unregister(self, name: str) -> bool:
        owner = _active_plugin_name()
        return self.registry.unregister(name, owner=owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class ToolsComponent:
    """Register the tool-layer capabilities as XCore services."""

    name = "xbot.tools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        runtime_config = ctx.runtime
        variables = ctx.variables
        workspace_root = ctx.workspace_root
        data_root = ctx.data_root
        state_store = ctx.state_store
        parent_permission_system = (config or {}).get("parent_permission_system")

        tool_registry = ToolRegistry()
        context_builder = ContextBuilder()
        agent_registry = AgentRegistry()
        sandbox = SandboxPolicy(
            runtime_config.sandbox,
            data_root=data_root,
            workspace_root=workspace_root,
            session_root=state_store.root,
            variables=variables,
        )
        permissions = PermissionSystem(
            runtime_config.permissions,
            variables=variables,
        )
        if parent_permission_system is not None:
            permissions = PermissionIntersection(parent_permission_system, permissions)
        job_registry = JobRegistry(
            limits={
                JobKind.SUBAGENT: runtime_config.max_concurrent_subagents,
            },
        )

        ctx.set("tools", ToolsService(tool_registry))
        ctx.set("commands", CommandsService())
        ctx.set("prompts", PromptsService(context_builder))
        ctx.set("sandbox", sandbox)
        ctx.set("permissions", permissions)
        ctx.set("job_registry", job_registry)
        ctx.set("agents", AgentsService(agent_registry))
        ctx.set("context_builder", context_builder)


plugin = ToolsComponent()
