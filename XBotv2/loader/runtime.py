"""Import and mount one resolved plugin tree before application startup."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from pydantic import BaseModel
from xcore import Context, FiberState, PluginHandle
from xcore.plugin import resolve_plugin

from XBotv2.loader.contracts import PluginEntry, PluginTree

logger = logging.getLogger("xbotv2.loader")


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
            logger.debug("plugin.skipped entry=%s reason=disabled", entry.id)
            continue
        try:
            plugin = _fresh_plugin(_import_plugin(entry.name), entry)
            mount_ctx = ctx
            for name, label in (entry.isolate or {}).items():
                mount_ctx = mount_ctx.isolate(
                    name,
                    label if label is not True else None,
                )
            handles[entry.id] = mount_ctx.plugin(plugin, entry.config)
        except Exception:
            logger.warning(
                "plugin.mount.failed entry=%s module=%s; skipping",
                entry.id, entry.name, exc_info=True,
            )
            continue
        logger.debug(
            "plugin.mounted entry=%s module=%s isolates=%s",
            entry.id,
            entry.name,
            sorted((entry.isolate or {}).keys()),
        )
    return handles


def report_plugin_activation(handles: tuple[PluginHandle, ...]) -> None:
    """Report deferred capabilities; XCore isolates and logs failed fibers."""
    for handle in handles:
        if handle.state is FiberState.FAILED:
            # Cancellation and process-exit signals are not optional-plugin
            # failures. Do not turn a cancelled boot into a running host.
            if handle.error is not None and not isinstance(handle.error, Exception):
                raise handle.error
            logger.warning(
                "plugin.activation.failed name=%s; skipping: %s",
                handle.name, handle.error,
            )
        elif handle.state is not FiberState.RUNNING:
            logger.info(
                "plugin.activation.deferred name=%s state=%s missing_dependencies=%s",
                handle.name, handle.state.value, list(handle.missing_dependencies),
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


def plugin_config_schema(entry: PluginEntry) -> Any:
    """Return the producer-declared schema for one resolved tree entry.

    Importing a plugin to inspect its declaration never mounts it or executes
    its lifecycle.  Configuration clients use this only to describe the same
    ``Config`` object that XCore validates during application boot.
    """
    plugin = _import_plugin(entry.name)
    definition = resolve_plugin(plugin)
    if definition is None:
        raise TypeError(f"plugin {entry.id!r} has no XCore plugin definition")
    return definition.config_schema


def validate_plugin_config(schema: Any, raw_config: Any) -> Any:
    """Validate an explicit configuration edit against the producer's schema.

    Activation validation belongs to XCore; generic tree reads do not call
    this across every plugin, which would defeat activation failure isolation.
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(raw_config or {})
    return raw_config


def plugin_config_json_schema(schema: Any) -> dict[str, Any]:
    """Project a plugin-owned Pydantic model into standard JSON Schema."""
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema(mode="serialization")
    raise TypeError("plugin does not declare a Pydantic config model")


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
    "plugin_config_json_schema",
    "plugin_config_schema",
    "resolve_plugin_from_module",
    "validate_plugin_config",
    "report_plugin_activation",
]
