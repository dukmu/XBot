"""Core tools component: base tools and core event listeners as a plugin.

Registers the always-available filesystem, shell, and content tools, the
tool-result cache event listener, and the
startup-configured hooks from the runtime config -- all through the shared
services, so even "core" setup is a plugin in the tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from XBotv2.core.events import Events
from XBotv2.core.tools import Tool

class CoreToolsComponent:
    inject = [
        "tools", "session", "storage", "sandbox", "jobs", "workspace_root",
    ]
    """Register base tools and core event listeners (mounted after tools)."""

    name = "xbot.coretools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        storage = ctx.storage
        result_config = dict(config.get("tool_results") or {})
        max_inline_chars = int(result_config.get("max_inline_chars", 12_000))
        preview_chars = int(result_config.get("preview_chars", 4_000))
        if max_inline_chars < 1 or preview_chars < 0:
            raise ValueError("Invalid tool result size limits")
        if preview_chars > max_inline_chars:
            raise ValueError("preview_chars cannot exceed max_inline_chars")
        workspace_xbot = Path(ctx.workspace_root) / ".xbot"
        hooks = [
            _declaration(item, workspace_xbot, hook=True)
            for item in config.get("hooks") or []
        ]
        workspace_tools = [
            _declaration(item, workspace_xbot)
            for item in config.get("workspace_tools") or []
        ]
        from XBotv2.coretools.filesystem import FILESYSTEM_TOOLS
        from XBotv2.coretools.shell import SHELL_TOOLS
        from XBotv2.coretools.result_cache import make_tool_result_cache_hook

        # ``read(mode=media)`` is the single model-facing content tool; it
        # covers path, URL, and base64 media input (images today).
        sandboxed_tools = [*FILESYSTEM_TOOLS, *SHELL_TOOLS]
        for tool in sandboxed_tools:
            if tool.name == "shell":
                injected = {
                    "sandbox": ctx.sandbox,
                    "job_registry": ctx.jobs,
                    "default_cwd": str(ctx.workspace_root),
                }
            elif tool in sandboxed_tools:
                injected = {"sandbox": ctx.sandbox, "job_registry": ctx.jobs}
            else:
                injected = None
            ctx.tools.register(tool, injected=injected)

        ctx.on(
            Events.AFTER_TOOLS,
            make_tool_result_cache_hook(
                storage,
                max_inline_chars=max_inline_chars,
                preview_chars=preview_chars,
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
                ctx.tools.register(tool, namespace="workspace")


@dataclass(frozen=True)
class _Declaration:
    target: str
    base_dir: Path
    stage: str = ""


def _declaration(
    raw: dict[str, Any],
    workspace_xbot: Path,
    *,
    hook: bool = False,
) -> _Declaration:
    target = str(raw.get("target") or "")
    source, separator, export = target.partition(":")
    if not separator or not source or not export:
        raise ValueError("target must use source:export syntax")
    stage = str(raw.get("stage") or "")
    if hook and not stage:
        raise ValueError("hook stage must not be empty")
    return _Declaration(
        target=target,
        base_dir=Path(raw.get("base_dir") or workspace_xbot),
        stage=stage,
    )


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
