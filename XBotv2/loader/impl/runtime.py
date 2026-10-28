"""Import and mount one resolved plugin tree before application startup."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any
from xcore.api.plugins import FiberState
from xcore.api.plugins import resolve_plugin
from XBotv2.loader.api.contracts import LoadError, PluginEntry

logger = logging.getLogger("xbotv2.loader")


def _module(name: str) -> Any:
    """Builtins have one implementation entry; external names are exact modules."""
    builtin = __import__("pathlib").Path(__file__).resolve().parents[2] / name
    module_name = f"XBotv2.{name}.impl.plugin" if builtin.is_dir() else name
    module = importlib.import_module(module_name)
    if not any(hasattr(module, f"mount_{kind}") for kind in ("process", "session", "thread")):
        raise ImportError(f"plugin module {module_name!r} declares no scope facets")
    return module


def _facet(entry: PluginEntry, kind: str) -> Any:
    module = _module(entry.name)
    facet = getattr(module, f"mount_{kind}", None)
    if facet is not None and not inspect.isfunction(facet):
        raise TypeError(f"{entry.name}.mount_{kind} must be a function")
    return facet


class Loader:
    """Discover declared facets and mount them in independently owned scopes."""

    def __init__(self, *, plugin_dirs=()):
        import sys
        self._import_paths = []
        for directory in plugin_dirs:
            path = str(__import__("pathlib").Path(directory).resolve())
            if path not in sys.path:
                sys.path.insert(0, path)
                self._import_paths.append(path)

    def close(self):
        import sys
        for path in self._import_paths:
            sys.path.remove(path)
        self._import_paths.clear()

    def config_schema(self, entry):
        return plugin_config_schema(entry)

    def scope(self, parent, name, *, kind, tree, provides=()):
        declared = list(provides)
        for entry in tree.entries:
            try:
                facet = _facet(entry, kind)
            except Exception as error:
                if not entry.optional:
                    raise LoadError(f"Cannot discover {entry.id} ({kind}): {error}") from error
                logger.warning("optional facet discovery failed entry=%s kind=%s: %s", entry.id, kind, error)
                continue
            if facet is not None:
                declared.extend(getattr(facet, "provide", ()))
        return parent.scope(name, provides=tuple(declared))

    async def mount(self, ctx, tree, *, kind, required=()):
        handles = {}
        try:
            for entry in tree.entries:
                if entry.disabled:
                    continue
                try:
                    facet = _facet(entry, kind)
                    if facet is None:
                        continue
                    if not hasattr(facet, "inject") or not hasattr(facet, "provide"):
                        raise TypeError("facets must declare inject and provide")
                    handles[entry.id] = ctx.plugin(facet, entry.config)
                except Exception as error:
                    if not entry.optional:
                        raise LoadError(f"Cannot mount {entry.id} ({kind}): {error}") from error
                    logger.warning("optional facet mount failed entry=%s kind=%s: %s", entry.id, kind, error)
            await ctx.start()
            failures = []
            errors = []
            for entry in tree.entries:
                handle = handles.get(entry.id)
                if handle is None:
                    continue
                facet = _facet(entry, kind)
                promised = tuple(getattr(facet, "provide", ()))
                absent = [str(key) for key in promised if not ctx.has(key)]
                activation_errors = handle.activation_errors
                if handle.state is FiberState.RUNNING and not absent and not activation_errors:
                    continue
                error = activation_errors[0][1] if activation_errors else handle.error
                if error is not None and not isinstance(error, Exception):
                    raise error
                if activation_errors:
                    detail = f"{entry.id}: " + "; ".join(f"{owner}: {type(failure).__name__}: {failure}" for owner, failure in activation_errors)
                elif error is not None:
                    detail = f"{entry.id}: {type(error).__name__}: {error}"
                else:
                    detail = f"{entry.id}: unresolved dependencies {tuple(handle.missing_dependencies)}, absent provides {absent} (missing or cyclic)"
                if entry.optional:
                    logger.warning("optional facet unavailable scope=%s kind=%s: %s", ctx.name, kind, detail)
                    await handle.dispose()
                else:
                    failures.append(detail)
                    if error is not None:
                        errors.append(error)
            missing = [str(key) for key in required if not ctx.has(key)]
            if failures or missing:
                raise LoadError(f"Cannot open {kind} scope: " + "; ".join([*failures, *([f"missing required services: {missing}"] if missing else [])])) from (errors[0] if errors else None)
        except BaseException:
            await ctx.close()
            raise


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


def _import_plugin(name: str) -> Any:
    module = _module(name)
    for kind in ("thread", "session", "process"):
        facet = getattr(module, f"mount_{kind}", None)
        if facet is not None:
            return facet
    raise ImportError(f"plugin {name!r} declares no mount_process/session/thread facet")
