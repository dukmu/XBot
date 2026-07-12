"""PluginStore: per-plugin isolated key-value store.

Each plugin gets its own namespace backed by CoreStateStore.
Core persists plugin states as opaque blobs and never reads or
interprets plugin data.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from xbotv2.persistence.store import CoreStateStore


class PluginStore:
    """Per-plugin key-value store backed by CoreStateStore.

    Each plugin gets its own namespace. The store is a thin wrapper
    that delegates persistence to CoreStateStore but enforces isolation.

    Usage::

        store = PluginStore(core_store, "planning")
        await store.set("active_node", "node-1")
        node = await store.get("active_node")
    """

    def __init__(self, core_store: "CoreStateStore", plugin_name: str) -> None:
        self._core = core_store
        self._name = plugin_name

    async def get(self, key: str, default: Any = None) -> Any:
        """Read a value from the plugin's namespace."""
        return self._core.get_plugin_state(self._name).get(key, default)

    async def set(self, key: str, value: Any) -> None:
        """Write a value to the plugin's namespace (persisted immediately)."""
        state = self._core.get_plugin_state(self._name)
        state[key] = value
        self._core.set_plugin_state(self._name, state)

    async def delete(self, key: str) -> None:
        """Remove a key from the plugin's namespace."""
        state = self._core.get_plugin_state(self._name)
        state.pop(key, None)
        self._core.set_plugin_state(self._name, state)

    async def all(self) -> dict[str, Any]:
        """Return all key-value pairs in the plugin's namespace."""
        return self._core.get_plugin_state(self._name)

    async def clear(self) -> None:
        """Remove all keys from the plugin's namespace."""
        self._core.set_plugin_state(self._name, {})
