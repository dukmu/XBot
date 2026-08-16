"""Core tools component: base tools and core event listeners as a plugin.

Registers the always-available base tools (filesystem/shell/content/
interaction), the tool-result cache event listener, and the
startup-configured hooks from the runtime config -- all through the shared
services, so even "core" setup is a plugin in the tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.core.events import Events
from XBotv2.core.tools import Tool

NON_INTERACTIVE_FORBIDDEN_TOOLS = frozenset({"ask_user", "request_permission"})


class CoreToolsComponent:
    inject = ['tools', 'session', 'state_store']
    """Register base tools and core event listeners (mounted after tools)."""

    name = "xbot.coretools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.config.models import (
            HookConfig,
            ToolResultConfig,
            WorkspaceToolConfig,
        )

        config = config or {}
        interactive = bool(config.get("interactive", True))
        tool_registry = ctx.tools.registry
        state_store = ctx.state_store
        tool_results = ToolResultConfig(**(config.get("tool_results") or {}))
        workspace_xbot = Path(ctx.workspace_root) / ".xbot"
        hooks = [
            _default_base_dir(HookConfig(**item), workspace_xbot)
            for item in config.get("hooks") or []
        ]
        workspace_tools = [
            _default_base_dir(WorkspaceToolConfig(**item), workspace_xbot)
            for item in config.get("workspace_tools") or []
        ]
        from XBotv2.coretools.filesystem import FILESYSTEM_TOOLS
        from XBotv2.coretools.shell import SHELL_TOOLS
        from XBotv2.coretools.content import content_read_tool
        from XBotv2.coretools.interaction import (
            ask_user,
            request_permission,
            send_message,
        )
        from XBotv2.coretools.result_cache import make_tool_result_cache_hook

        base_tools = [
            *((tool, "sandboxed") for tool in FILESYSTEM_TOOLS),
            *(
                (tool, "sandboxed" if tool.name in {"shell", "start_shell"} else "host")
                for tool in SHELL_TOOLS
            ),
            (content_read_tool, "sandboxed"),
            (send_message, "host"),
            (ask_user, "host"),
            (request_permission, "host"),
        ]
        for tool, sandbox_mode in base_tools:
            if not interactive and tool.name in NON_INTERACTIVE_FORBIDDEN_TOOLS:
                continue
            ctx.tools.register(tool, sandbox_mode=sandbox_mode)

        ctx.on(
            Events.AFTER_TOOLS,
            make_tool_result_cache_hook(
                state_store,
                max_inline_chars=tool_results.max_inline_chars,
                preview_chars=tool_results.preview_chars,
            ),
        )
        for declaration in hooks:
            ctx.on(
                declaration.stage,
                _resolve_hook_target(declaration),
            )
        for declaration in workspace_tools:
            exported = _resolve_workspace_target(declaration, directory="tools")
            tools = exported if isinstance(exported, (list, tuple)) else (exported,)
            if not tools:
                raise ValueError(
                    f"Workspace Tool export is empty: {declaration.target}"
                )
            for tool in tools:
                if not isinstance(tool, Tool):
                    raise TypeError(
                        f"Workspace Tool export {declaration.target!r} must contain "
                        "XBotv2.core.Tool values"
                    )
                ctx.tools.register(tool, namespace="workspace", sandbox_mode="host")


def _default_base_dir(declaration: Any, workspace_xbot: Path) -> Any:
    """Workspace hooks/tools default to ``<workspace>/.xbot`` when undeclared."""
    if getattr(declaration, "base_dir", None) is None:
        try:
            return declaration.model_copy(update={"base_dir": workspace_xbot})
        except Exception:  # noqa: BLE001 - dict-like fallback
            if isinstance(declaration, dict):
                return {**declaration, "base_dir": workspace_xbot}
    return declaration


def _resolve_hook_target(declaration: Any) -> Any:
    """Resolve a module or workspace script target without changing sys.path."""
    import importlib

    source, attr_name = declaration.target.split(":", 1)
    if source.endswith(".py") or "/" in source or "\\" in source:
        callback = _resolve_workspace_target(declaration, directory="hooks")
    else:
        module = importlib.import_module(source)
        try:
            callback = getattr(module, attr_name)
        except AttributeError as exc:
            raise ImportError(
                f"Hook target {declaration.target!r} does not exist"
            ) from exc
    if not callable(callback):
        raise TypeError(f"Hook target {declaration.target!r} is not callable")
    return callback


def _resolve_workspace_target(declaration: Any, *, directory: str) -> Any:
    """Load one declared export from a standard workspace extension directory."""
    import importlib.util
    from pathlib import Path

    source, attr_name = declaration.target.split(":", 1)
    base_dir = declaration.base_dir
    if base_dir is None:
        raise ValueError(
            f"Workspace {directory} target {source!r} must be declared in the workspace overlay"
        )
    extension_dir = (Path(base_dir) / directory).resolve()
    path = (Path(base_dir) / source).resolve()
    try:
        path.relative_to(extension_dir)
    except ValueError as exc:
        raise ValueError(
            f"Workspace {directory} scripts must stay inside .xbot/{directory}"
        ) from exc
    if not path.is_file():
        raise FileNotFoundError(f"Workspace {directory} script not found: {path}")
    spec = importlib.util.spec_from_file_location(
        f"xbotv2_workspace_{directory}_{abs(hash(path))}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load workspace {directory} script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(
            f"Workspace target {declaration.target!r} does not exist"
        ) from exc


plugin = CoreToolsComponent()
