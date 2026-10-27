"""Plugin lifecycle types and registration helpers."""

from xcore.plugin import (
    FiberState, PluginDef, PluginHandle, Registry, bound_effect,
    current_fiber, current_plugin_name, resolve_plugin,
)
