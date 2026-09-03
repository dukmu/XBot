"""Agent-loop tool registry with namespace-aware registration and filtering.

Namespaced tools use the format ``namespace:name``. Built-in tools are
bare names (default namespace).

``restrict()`` supports namespace patterns:
  - ``"*"`` or ``None``: all tools
  - ``"shell"``: bare name match (backwards-compat)
  - ``"filesystem*"``: all tools with that prefix
  - ``"skills:*"``: all discovered skill tools
  - ``"skills:global:*"``: all global skill tools
  - ``"mcp:github:*"``: all tools from one MCP server
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core.tools import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ToolRegistration] = {}
        self._enabled_names: set[str] | None = None

    def register(
        self,
        tool: Tool,
        *,
        namespace: str | None = None,
        model_visible: bool = True,
        timeout_seconds: float | None = None,
    ) -> str:
        name = tool.name
        ns = namespace or "builtin"
        full_name = name if ns == "builtin" else f"{ns}:{name}"
        if full_name in self._entries:
            raise ValueError(f"Tool {full_name!r} is already registered")
        duplicate = next(
            (
                entry.registered_name
                for entry in self._entries.values()
                if entry.tool.name == name
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError(
                f"Tool name {name!r} is already registered as {duplicate!r}"
            )
        self._entries[full_name] = ToolRegistration(
            tool=tool,
            registered_name=full_name,
            namespace=ns,
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
        )
        return full_name

    def unregister(self, name: str) -> bool:
        if name not in self._entries:
            return False
        del self._entries[name]
        if self._enabled_names is not None:
            self._enabled_names.discard(name)
        return True

    def get(self, name: str) -> ToolRegistration | None:
        return next(
            (
                entry
                for entry in self._matches(name)
                if entry.model_visible and self._is_enabled(entry.registered_name)
            ),
            None,
        )

    def get_registered(self, name: str) -> ToolRegistration | None:
        """Resolve an entry without applying model-visible tool restrictions."""
        return next(self._matches(name), None)

    def registered(self, name: str) -> bool:
        return name in self._entries and (self._enabled_names is None or name in self._enabled_names)

    def get_all(self) -> list[Tool]:
        return [
            entry.tool
            for name, entry in self._entries.items()
            if entry.model_visible and self._is_enabled(name)
        ]

    def names(self) -> list[str]:
        return [name for name in self._entries if self._is_enabled(name)]

    def registered_names(self) -> list[str]:
        return list(self._entries)

    def registered_entries(self) -> tuple[ToolRegistration, ...]:
        """Return all registered tools in registration order."""
        return tuple(self._entries.values())

    def restrict(self, tool_names: list[str] | None) -> list[str]:
        if tool_names is None:
            self._enabled_names = None
            return self.names()

        expanded: set[str] = set()
        for selector in tool_names:
            matches = self._expand_selector(selector)
            expanded.update(matches)
        self._enabled_names = expanded
        return self.names()

    def exclude(self, tool_names: list[str]) -> list[str]:
        """Hide tools matching selectors from the currently enabled set."""
        disabled = {
            name
            for selector in tool_names
            for name in self._expand_selector(selector)
        }
        self._enabled_names = set(self.names()) - disabled
        return self.names()

    def __len__(self) -> int:
        return len(self.names())

    def __contains__(self, name: str) -> bool:
        return self.registered(name)

    def _expand_selector(self, selector: str) -> list[str]:
        if selector in self._entries:
            return [selector]

        if ":" in selector:
            ns, _, name_part = selector.partition(":")
            ns_re = _wildcard_to_regex(ns)
            name_re = _wildcard_to_regex(name_part)
            pattern = re.compile(f"^{ns_re}:{name_re}$")
            return sorted(n for n in self._entries if pattern.match(n))

        if selector.endswith("*"):
            prefix = selector.rstrip("*")
            return sorted(n for n in self._entries if n.startswith(prefix))

        prefix_matches = [n for n in self._entries if n == selector or (":" not in n and n.startswith(selector))]
        if prefix_matches:
            return sorted(prefix_matches)
        return sorted(n for n in self._entries if n.endswith(f":{selector}"))

    def _is_enabled(self, name: str) -> bool:
        return self._enabled_names is None or name in self._enabled_names

    def _matches(self, name: str) -> Iterator[ToolRegistration]:
        exact = self._entries.get(name)
        if exact is not None:
            yield exact
        yield from (
            entry
            for entry in self._entries.values()
            if entry is not exact and entry.tool.name == name
        )


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
