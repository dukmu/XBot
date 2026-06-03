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
        self._cache: dict[str, Any] | None = None

    async def get(self, key: str, default: Any = None) -> Any:
        """Read a value from the plugin's namespace."""
        self._ensure_loaded()
        return self._cache.get(key, default)  # type: ignore[union-attr]

    async def set(self, key: str, value: Any) -> None:
        """Write a value to the plugin's namespace (persisted immediately)."""
        self._ensure_loaded()
        self._cache[key] = value  # type: ignore[index]
        self._core.set_plugin_state(self._name, self._cache)  # type: ignore[index]

    async def delete(self, key: str) -> None:
        """Remove a key from the plugin's namespace."""
        self._ensure_loaded()
        self._cache.pop(key, None)  # type: ignore[union-attr]
        self._core.set_plugin_state(self._name, self._cache)  # type: ignore[index]

    async def all(self) -> dict[str, Any]:
        """Return all key-value pairs in the plugin's namespace."""
        self._ensure_loaded()
        return dict(self._cache)  # type: ignore[arg-type]

    async def clear(self) -> None:
        """Remove all keys from the plugin's namespace."""
        self._cache = {}
        self._core.set_plugin_state(self._name, {})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._cache is None:
            self._cache = self._core.get_plugin_state(self._name)
