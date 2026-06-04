"""Extensible tool registry with plugin ownership tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RegisteredSandboxMode = Literal["sandboxed", "host"]
RegisteredExecutionMode = Literal["parallel", "sequential"]


@dataclass
class ToolEntry:
    """Metadata for one registered tool."""

    tool: Any  # BaseTool instance
    sandbox_mode: RegisteredSandboxMode = "host"
    execution_mode: RegisteredExecutionMode = "sequential"
    lock_fields: tuple[str, ...] = ()
    owner_plugin: str | None = None


class ToolRegistry:
    """Pluggable tool registry.

    Maps tool names to ToolEntry objects. Supports:
    - Registration with sandbox/execution metadata
    - Wildcard expansion for tool groups (e.g. "filesystem" → filesystem_read, write, list)
    - Plugin ownership tracking for unload/reload
    - Filtering by enabled tool list

    Usage::

        registry = ToolRegistry()
        registry.register(filesystem_read, sandbox_mode="sandboxed")
        tools = registry.filter(["filesystem_read", "shell"])
    """

    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}
        self._enabled_names: set[str] | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        tool: Any,
        *,
        sandbox_mode: RegisteredSandboxMode = "host",
        execution_mode: RegisteredExecutionMode = "sequential",
        lock_fields: tuple[str, ...] = (),
        owner_plugin: str | None = None,
    ) -> None:
        """Register a tool."""
        name = tool.name if hasattr(tool, "name") else getattr(tool, "__name__", str(tool))
        self._entries[name] = ToolEntry(
            tool=tool,
            sandbox_mode=sandbox_mode,
            execution_mode=execution_mode,
            lock_fields=lock_fields,
            owner_plugin=owner_plugin,
        )

    def register_many(
        self,
        tools: list[Any],
        *,
        sandbox_modes: dict[str, RegisteredSandboxMode] | None = None,
        execution_modes: dict[str, RegisteredExecutionMode] | None = None,
        lock_fields: dict[str, tuple[str, ...]] | None = None,
        owner_plugin: str | None = None,
    ) -> None:
        """Batch-register tools."""
        sandbox_modes = sandbox_modes or {}
        execution_modes = execution_modes or {}
        lock_fields_map = lock_fields or {}

        for tool in tools:
            name = tool.name if hasattr(tool, "name") else getattr(tool, "__name__", str(tool))
            self.register(
                tool,
                sandbox_mode=sandbox_modes.get(name, "host"),
                execution_mode=execution_modes.get(name, "sequential"),
                lock_fields=lock_fields_map.get(name, ()),
                owner_plugin=owner_plugin,
            )

    def unregister_plugin_tools(self, plugin_name: str) -> list[str]:
        """Remove all tools owned by *plugin_name*. Returns removed names."""
        removed = [
            name
            for name, entry in self._entries.items()
            if entry.owner_plugin == plugin_name
        ]
        for name in removed:
            del self._entries[name]
        return removed

    def unregister(self, name: str) -> bool:
        """Remove one registered tool by name."""
        if name not in self._entries:
            return False
        del self._entries[name]
        if self._enabled_names is not None:
            self._enabled_names.discard(name)
        return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolEntry | None:
        """Return the ToolEntry for *name*, or None."""
        if self._enabled_names is not None and name not in self._enabled_names:
            return None
        return self._entries.get(name)

    def registered(self, name: str) -> bool:
        """Return whether *name* is registered."""
        return name in self._entries and (
            self._enabled_names is None or name in self._enabled_names
        )

    def get_all(self) -> list[Any]:
        """Return all registered tool instances."""
        return [entry.tool for name, entry in self._entries.items() if self._is_enabled(name)]

    def names(self) -> list[str]:
        """Return all registered tool names."""
        return [name for name in self._entries if self._is_enabled(name)]

    def registered_names(self) -> list[str]:
        """Return all registered tool names, ignoring visibility restrictions."""
        return list(self._entries)

    def sandbox_modes(self) -> dict[str, RegisteredSandboxMode]:
        """Return {tool_name: sandbox_mode} for all registered tools."""
        return {
            name: entry.sandbox_mode
            for name, entry in self._entries.items()
            if self._is_enabled(name)
        }

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter(self, tool_names: list[str] | None) -> list[Any]:
        """Return tool instances for the requested names.

        If *tool_names* is None or empty, return all tools.

        Supports wildcard expansion: "filesystem*" or bare "filesystem" expands
        to filesystem_read, filesystem_write, filesystem_list.
        """
        if not tool_names:
            return self.get_all()

        result: list[Any] = []
        seen: set[str] = set()

        for name in tool_names:
            # Determine prefix for matching
            if name.endswith("*"):
                prefix = name.rstrip("*")
            else:
                prefix = name

            # Try exact match first
            if name in self._entries and name not in seen:
                result.append(self._entries[name].tool)
                seen.add(name)
                continue

            # Try prefix expansion (for bare "filesystem" or explicit "filesystem*")
            for entry_name, entry in self._entries.items():
                if entry_name.startswith(prefix) and entry_name not in seen:
                    result.append(entry.tool)
                    seen.add(entry_name)

        return result

    def restrict(self, tool_names: list[str] | None) -> list[str]:
        """Restrict visible/executable tools to the expanded tool names.

        Raises ValueError if any requested selector does not match a registered
        tool. The existing ``filter`` method remains a pure query helper.
        """
        if not tool_names:
            self._enabled_names = None
            return self.names()

        expanded: set[str] = set()
        missing: list[str] = []
        for selector in tool_names:
            matches = self._expand_selector(selector)
            if not matches:
                missing.append(selector)
                continue
            expanded.update(matches)

        if missing:
            raise ValueError(f"Unknown tool selector(s): {', '.join(missing)}")

        self._enabled_names = expanded
        return self.names()

    def __len__(self) -> int:
        return len(self.names())

    def __contains__(self, name: str) -> bool:
        return self.registered(name)

    def _expand_selector(self, selector: str) -> list[str]:
        if selector.endswith("*"):
            prefix = selector.rstrip("*")
        else:
            prefix = selector

        if selector in self._entries:
            return [selector]
        return [name for name in self._entries if name.startswith(prefix)]

    def _is_enabled(self, name: str) -> bool:
        return self._enabled_names is None or name in self._enabled_names
