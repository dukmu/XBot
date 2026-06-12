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

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolEntry | None:
        """Return the ToolEntry for *name*, or None."""
        return self._entries.get(name)

    def registered(self, name: str) -> bool:
        """Return whether *name* is registered."""
        return name in self._entries

    def get_all(self) -> list[Any]:
        """Return all registered tool instances."""
        return [e.tool for e in self._entries.values()]

    def names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._entries.keys())

    def sandbox_modes(self) -> dict[str, RegisteredSandboxMode]:
        """Return {tool_name: sandbox_mode} for all registered tools."""
        return {name: entry.sandbox_mode for name, entry in self._entries.items()}

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

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._entries
