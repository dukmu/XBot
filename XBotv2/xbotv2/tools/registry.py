"""Extensible tool registry with namespace-aware registration and filtering.

Namespaced tools use the format ``namespace:name``. Built-in tools are
bare names (default namespace). Plugin tools use the plugin name as
namespace. MCP tools use ``mcp.<server>`` as namespace.

``restrict()`` supports namespace patterns:
  - ``"*"`` or ``None``: all tools
  - ``"shell"``: bare name match (backwards-compat)
  - ``"builtin:*"``: all tools with namespace ``builtin``
  - ``"skills:*"``: all skills plugin tools
  - ``"skills:skill"``: specific namespaced tool
  - ``"mcp.*:*"``: wildcard namespace match
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

RegisteredSandboxMode = Literal["sandboxed", "host"]
RegisteredExecutionMode = Literal["parallel", "sequential"]


@dataclass
class ToolEntry:
    tool: Any
    sandbox_mode: RegisteredSandboxMode = "host"
    execution_mode: RegisteredExecutionMode = "sequential"
    lock_fields: tuple[str, ...] = ()
    owner_plugin: str | None = None
    namespace: str = "builtin"


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}
        self._enabled_names: set[str] | None = None

    def register(
        self,
        tool: Any,
        *,
        sandbox_mode: RegisteredSandboxMode = "host",
        execution_mode: RegisteredExecutionMode = "sequential",
        lock_fields: tuple[str, ...] = (),
        owner_plugin: str | None = None,
        namespace: str | None = None,
    ) -> None:
        name = tool.name if hasattr(tool, "name") else getattr(tool, "__name__", str(tool))
        ns = namespace or "builtin"
        full_name = f"{ns}:{name}" if ns != "builtin" else name
        self._entries[full_name] = ToolEntry(
            tool=tool,
            sandbox_mode=sandbox_mode,
            execution_mode=execution_mode,
            lock_fields=lock_fields,
            owner_plugin=owner_plugin,
            namespace=ns,
        )

    def register_many(
        self, tools: list[Any], *,
        sandbox_modes: dict[str, RegisteredSandboxMode] | None = None,
        execution_modes: dict[str, RegisteredExecutionMode] | None = None,
        lock_fields: dict[str, tuple[str, ...]] | None = None,
        owner_plugin: str | None = None,
        namespace: str | None = None,
    ) -> None:
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
                namespace=namespace,
            )

    def unregister_plugin_tools(self, plugin_name: str) -> list[str]:
        removed = [n for n, e in self._entries.items() if e.owner_plugin == plugin_name]
        for name in removed:
            del self._entries[name]
        return removed

    def unregister(self, name: str) -> bool:
        if name not in self._entries:
            return False
        del self._entries[name]
        if self._enabled_names is not None:
            self._enabled_names.discard(name)
        return True

    def get(self, name: str) -> ToolEntry | None:
        if name in self._entries and self._is_enabled(name):
            return self._entries[name]
        # Fallback: match by tool display name (XBotTool.name attribute)
        for full_name, entry in self._entries.items():
            if getattr(entry.tool, "name", "") == name and self._is_enabled(full_name):
                return entry
        return None

    def registered(self, name: str) -> bool:
        return name in self._entries and (self._enabled_names is None or name in self._enabled_names)

    def get_all(self) -> list[Any]:
        return [e.tool for name, e in self._entries.items() if self._is_enabled(name)]

    def names(self) -> list[str]:
        return [name for name in self._entries if self._is_enabled(name)]

    def registered_names(self) -> list[str]:
        return list(self._entries)

    def sandbox_modes(self) -> dict[str, RegisteredSandboxMode]:
        return {name: e.sandbox_mode for name, e in self._entries.items() if self._is_enabled(name)}

    def filter(self, tool_names: list[str] | None) -> list[Any]:
        if not tool_names:
            return self.get_all()
        expanded = self._expand_selectors(tool_names)
        return [self._entries[name].tool for name in expanded if name in self._entries]

    def restrict(self, tool_names: list[str] | None) -> list[str]:
        if not tool_names:
            self._enabled_names = None
            return self.names()

        expanded: set[str] = set()
        for selector in tool_names:
            matches = self._expand_selector(selector)
            expanded.update(matches)
        self._enabled_names = expanded
        return self.names()

    def __len__(self) -> int:
        return len(self.names())

    def __contains__(self, name: str) -> bool:
        return self.registered(name)

    def _expand_selectors(self, selectors: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for sel in selectors:
            for name in self._expand_selector(sel):
                if name not in seen:
                    result.append(name)
                    seen.add(name)
        return result

    def _expand_selector(self, selector: str) -> list[str]:
        if selector in self._entries:
            return [selector]

        if ":" in selector:
            ns, _, name_part = selector.partition(":")
            ns_re = _wildcard_to_regex(ns)
            name_re = _wildcard_to_regex(name_part) if name_part != "*" else ".*"
            pattern = re.compile(f"^{ns_re}:{name_re}$")
            return sorted(n for n in self._entries if pattern.match(n))

        # Bare selector with wildcard: strip * for prefix matching
        if selector.endswith("*"):
            prefix = selector.rstrip("*")
            return sorted(n for n in self._entries if n.startswith(prefix))

        # Bare selector without wildcard: exact match or bare-prefix match
        prefix_matches = [n for n in self._entries if n == selector or ("::" not in n and n.startswith(selector))]
        if prefix_matches:
            return sorted(prefix_matches)
        return sorted(n for n in self._entries if n.endswith(f":{selector}"))

    def _is_enabled(self, name: str) -> bool:
        return self._enabled_names is None or name in self._enabled_names


def _wildcard_to_regex(pattern: str) -> str:
    result = []
    for ch in pattern:
        if ch == "*":
            result.append(".*")
        elif ch == "?":
            result.append(".")
        elif ch in r".^$+?{}[]|()\\":
            result.append("\\" + ch)
        else:
            result.append(ch)
    return "".join(result)
