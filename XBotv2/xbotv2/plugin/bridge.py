"""XBotv2 <-> XCore migration bridge.

Makes XBotv2's core components available as XCore services and adapts
:class:`~xbotv2.api.plugins.PluginBase` plugins to XCore object plugins
(design: ``XCore/docs/05-migration-plan.md``).

Key mechanisms:

- **Core components are services**: ``ctx.tools`` / ``ctx.commands`` /
  ``ctx.prompts`` / ``ctx.agents`` / ``ctx.job_registry`` /
  ``ctx.variables`` / ``ctx.session`` / ``ctx.runtime`` /
  ``ctx.agent_runtime`` / ``ctx.paths``.
- **Registration is a fiber effect**: anything a plugin registers through
  these services is undone automatically when the plugin fiber unloads
  (replacing the loader's manual rollback tables).
- **Caller tracking**: a ``contextvars.ContextVar`` holds the currently
  executing plugin context (during ``apply`` and, via the runtime context,
  during plugin hooks) so service methods know which fiber owns a
  registration -- the Cordis "caller context" pattern.
- **``plugin_runtime``**: plugins (skills, MCP) register dynamic tools and
  commands inside hooks through ``ctx.plugin_runtime``; the bridge derives the
  runtime context from the hook's owning plugin fiber.
"""

from __future__ import annotations

import contextvars
import inspect
import logging
from pathlib import Path
from typing import Any, Callable

from xcore import Context

from xbotv2.api.commands import Command
from xbotv2.api.plugins import PluginBase, ToolRegistrationOptions
from xbotv2.api.tools import Tool

logger = logging.getLogger("xbotv2.plugin_bridge")

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


def register_core_services(
    ctx: Context,
    *,
    tool_registry: Any,
    context_builder: Any,
    agent_registry: Any,
    job_registry: Any,
    runtime_variables: Any,
    workspace_root: str | Path,
    data_root: str | Path,
    session: Any,
    runtime_config: Any,
    agent_runtime: Any,
    paths: Any,
) -> None:
    """Register XBotv2 core components as XCore services on ``ctx``.

    Service names match the plugin-facing attributes of the pre-migration
    setup context (``ctx.variables`` / ``ctx.workspace_root`` /
    ``ctx.data_root`` / ``ctx.agent_runtime`` / ``ctx.job_registry``) plus
    the capability registries (``ctx.tools`` / ``ctx.commands`` /
    ``ctx.prompts`` / ``ctx.agents``) and read-only info services.
    ``ctx.state`` is XCore-managed (created at ``data_dir/state.json`` from
    the context's ``data_dir``), so it is not registered here.
    """
    ctx.set("tools", ToolsService(tool_registry))
    ctx.set("commands", CommandsService())
    ctx.set("prompts", PromptsService(context_builder))
    ctx.set("agents", AgentsService(agent_registry))
    ctx.set("job_registry", job_registry)
    ctx.set("variables", runtime_variables)
    ctx.set("workspace_root", Path(workspace_root))
    ctx.set("data_root", Path(data_root))
    ctx.set("session", session)
    ctx.set("runtime", runtime_config)
    ctx.set("agent_runtime", agent_runtime)
    ctx.set("paths", paths)


# ---------------------------------------------------------------------------
# Plugin runtime context (``ctx.plugin_runtime`` inside hooks)
# ---------------------------------------------------------------------------


class RuntimePluginContext:
    """Runtime capabilities of one plugin, available during its hooks.

    Registering through this context attaches fiber-scoped cleanup, so
    dynamic tools/commands registered mid-turn are removed on plugin unload.
    """

    def __init__(
        self,
        *,
        plugin_name: str,
        ctx: Any,
        tools: ToolsService,
        commands: CommandsService,
    ) -> None:
        self._plugin_name = plugin_name
        self._ctx = ctx
        self._tools = tools
        self._commands = commands

    def register_tool(
        self,
        tool: Tool,
        options: ToolRegistrationOptions | None = None,
    ) -> str:
        return _with_plugin_ctx(self._ctx, lambda: self._tools.register(tool, options))

    def unregister_tool(self, registered_name: str) -> bool:
        return self._tools.unregister(registered_name)

    def register_command(self, command: Command) -> str:
        return _with_plugin_ctx(
            self._ctx, lambda: self._commands.register(command)
        )

    def unregister_command(self, name: str) -> bool:
        return self._commands.unregister(name)


def plugin_runtime_for(owner_ctx: Any) -> RuntimePluginContext | None:
    """Resolve the runtime context of a hook's owning plugin fiber.

    Returns ``None`` for core (non-plugin) hooks and root contexts.
    """
    fiber = getattr(owner_ctx, "fiber", None)
    runtime = getattr(fiber, "runtime", None)
    if runtime is None:
        return None
    return RuntimePluginContext(
        plugin_name=runtime.definition.name,
        ctx=owner_ctx,
        tools=owner_ctx.tools,
        commands=owner_ctx.commands,
    )


# ---------------------------------------------------------------------------
# Plugin adapter (PluginBase -> XCore object plugin)
# ---------------------------------------------------------------------------


class PluginAdapter:
    """Adapts a :class:`PluginBase` instance to an XCore object plugin.

    The adapter binds ``plugin.ctx`` / ``plugin.store``
    (``ctx.state.namespace(name)``), registers ``on_unload`` as a disposer,
    and keeps the caller-tracking contextvar set across the whole (possibly
    async) apply body.
    """

    def __init__(self, plugin: PluginBase) -> None:
        self._plugin = plugin
        self.name = plugin.manifest.name

    async def apply(self, ctx: Context, config: Any) -> Any:
        plugin = self._plugin
        plugin.ctx = ctx
        plugin.store = ctx.state.namespace(plugin.manifest.name)
        ctx.dispose(plugin.on_unload)
        token = _active_ctx.set(ctx)
        try:
            result = plugin.apply(ctx)
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            _active_ctx.reset(token)
