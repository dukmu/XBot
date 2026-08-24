"""Import and mount one resolved plugin tree before application startup."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from xcore import Context, FiberState, PluginHandle

from XBotv2.loader.types import LoadError, PluginEntry, PluginTree

logger = logging.getLogger("loader")


def resolve_plugin_from_module(module: Any, name: str) -> Any:
    """Resolve the conventional plugin export from an imported module."""
    candidate = getattr(module, "plugin", None)
    if _is_module(candidate):
        candidate = getattr(candidate, "plugin", None)
    if candidate is not None:
        return candidate
    candidate = getattr(module, "Plugin", None)
    if _is_module(candidate):
        candidate = getattr(candidate, "Plugin", None)
    if candidate is not None:
        return candidate
    if callable(getattr(module, "apply", None)) or callable(module):
        return module
    raise ImportError(
        f"plugin module {name!r} must export 'plugin', 'Plugin', or be a plugin itself"
    )


def mount_plugin_tree(
    ctx: Context,
    tree: PluginTree,
) -> dict[str, PluginHandle]:
    """Mount every enabled entry on an inactive application context."""
    if ctx.is_active:
        raise RuntimeError("plugin tree must be mounted before Context.start()")

    handles: dict[str, PluginHandle] = {}
    for entry in tree.entries:
        if entry.disabled:
            logger.info("loader: entry %s disabled, skipping", entry.id)
            continue
        plugin = _fresh_plugin(_import_plugin(entry.name), entry)
        mount_ctx = ctx
        for name, label in (entry.isolate or {}).items():
            mount_ctx = mount_ctx.isolate(
                name,
                label if label is not True else None,
            )
        handles[entry.id] = mount_ctx.plugin(plugin, entry.config)
        logger.info("loader: mounted entry %s (%s)", entry.id, entry.name)
    return handles


def validate_mounted_tree(handles: dict[str, PluginHandle]) -> None:
    """Fail startup when a mounted plugin failed or has unmet dependencies."""
    for entry_id, handle in handles.items():
        if handle.state is FiberState.FAILED:
            assert handle.error is not None
            raise handle.error
    for entry_id, handle in handles.items():
        if handle.state is not FiberState.RUNNING:
            raise LoadError(
                f"plugin {entry_id!r} did not activate; "
                "unmet inject dependencies: "
                f"{list(handle.missing_dependencies)}"
            )


def _fresh_plugin(plugin: Any, entry: PluginEntry) -> Any:
    if inspect.isfunction(plugin) or inspect.isclass(plugin) or _is_module(plugin):
        return plugin
    try:
        return type(plugin)()
    except TypeError as error:
        raise TypeError(
            f"plugin {entry.id!r} exports a shared object that cannot be "
            "constructed without arguments; export a plugin class or function"
        ) from error


def _import_plugin(name: str) -> Any:
    candidates = (
        f"XBotv2.{name}.plugin",
        f"{name}.plugin",
        f"XBotv2.{name}",
        name,
    )
    invalid: list[str] = []
    for candidate in candidates:
        try:
            module = importlib.import_module(candidate)
        except ModuleNotFoundError as error:
            if error.name != candidate and not candidate.startswith(
                f"{error.name}."
            ):
                raise
            continue
        try:
            return resolve_plugin_from_module(module, name)
        except ImportError:
            invalid.append(
                f"{candidate} ({getattr(module, '__file__', 'unknown origin')})"
            )
            continue
    detail = (
        f"; imported non-plugin modules: {', '.join(invalid)}"
        if invalid
        else ""
    )
    raise ImportError(
        f"plugin {name!r} must export 'plugin', 'Plugin', or be a plugin itself"
        f"{detail}"
    )


def _is_module(value: Any) -> bool:
    import types

    return isinstance(value, types.ModuleType)


__all__ = [
    "mount_plugin_tree",
    "resolve_plugin_from_module",
    "validate_mounted_tree",
]
