"""Public declarations for plugin-tree loading."""

from XBotv2.loader.contracts import LoadError, PluginEntry, PluginOverlay, PluginTree
from XBotv2.loader.resolve import DEFAULT_TREE, resolve_agent_tree
from XBotv2.loader.runtime import (
    plugin_config_json_schema,
    plugin_config_schema,
    validate_plugin_config,
)

__all__ = [
    "LoadError",
    "PluginEntry",
    "PluginOverlay",
    "PluginTree",
    "DEFAULT_TREE",
    "resolve_agent_tree",
    "plugin_config_schema",
    "plugin_config_json_schema",
    "validate_plugin_config",
]
