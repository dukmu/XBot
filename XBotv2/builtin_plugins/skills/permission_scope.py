"""Per-turn tool permission overrides from active skills."""

from __future__ import annotations

import re
from typing import Any


class SkillPermissionScope:
    def __init__(self) -> None:
        self._allowed: list[re.Pattern[str]] = []
        self._disallowed: list[re.Pattern[str]] = []

    def add(
        self,
        allowed: list[str] | None = None,
        disallowed: list[str] | None = None,
    ) -> None:
        allowed_patterns = [_compile_pattern(p) for p in allowed or []]
        disallowed_patterns = [_compile_pattern(p) for p in disallowed or []]
        self._allowed.extend(allowed_patterns)
        self._disallowed.extend(disallowed_patterns)

    def check(self, tool_name: str, args: dict[str, Any] | None = None) -> str | None:
        targets = [tool_name]
        command = (args or {}).get("command")
        if isinstance(command, str):
            targets.append(f"{tool_name}({command})")

        for pattern in reversed(self._disallowed):
            if any(pattern.search(target) for target in targets):
                return "deny"
        for pattern in reversed(self._allowed):
            if any(pattern.search(target) for target in targets):
                return "allow"
        if self._allowed:
            return "deny"
        return None

    def clear(self) -> None:
        self._allowed.clear()
        self._disallowed.clear()


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    if not isinstance(pattern, str):
        raise ValueError("tool permission patterns must be strings")
    text = pattern.strip()
    if not text:
        raise ValueError("tool permission patterns must not be empty")
    # "shell(git *)" matches the shell tool's command argument.
    if "(" in text:
        base, inner = text.split("(", 1)
        if (
            not base
            or not text.endswith(")")
            or text.count("(") != text.count(")")
        ):
            raise ValueError(f"invalid tool permission pattern: {pattern!r}")
        base = re.escape(base)
        inner = inner[:-1].strip()
        inner_re = _wildcard_to_regex(inner)
        return re.compile(f"^{base}\\({inner_re}\\)$")
    if ")" in text:
        raise ValueError(f"invalid tool permission pattern: {pattern!r}")
    # "shell" or "mcp__*" matches the canonical tool name only.
    name_re = _wildcard_to_regex(text)
    return re.compile(f"^{name_re}$")


def validate_tool_patterns(patterns: list[str]) -> None:
    for pattern in patterns:
        _compile_pattern(pattern)


def _wildcard_to_regex(pattern: str) -> str:
    result = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            result.append(".*")
        elif ch == "?":
            result.append(".")
        elif ch in r".^$+?{}[]|()\\":
            result.append("\\" + ch)
        else:
            result.append(ch)
        i += 1
    return "".join(result)
