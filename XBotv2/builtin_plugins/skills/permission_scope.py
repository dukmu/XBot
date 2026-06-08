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
        for p in allowed or []:
            self._allowed.append(_compile_pattern(p))
        for p in disallowed or []:
            self._disallowed.append(_compile_pattern(p))

    def check(self, tool_name: str) -> str | None:
        for pattern in reversed(self._disallowed):
            if pattern.search(tool_name):
                return "deny"
        for pattern in reversed(self._allowed):
            if pattern.search(tool_name):
                return "allow"
        return None

    def clear(self) -> None:
        self._allowed.clear()
        self._disallowed.clear()


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    text = pattern.strip()
    if not text:
        return re.compile("^$")
    # "Bash(git *)" → match tool name + params
    if "(" in text:
        base = re.escape(text.split("(")[0])
        inner = text[text.index("(") + 1:text.rindex(")")].strip()
        inner_re = _wildcard_to_regex(inner)
        return re.compile(f"^{base}\\({inner_re}\\)$")
    # "Bash" or "mcp__*" → match tool name only
    name_re = _wildcard_to_regex(text)
    return re.compile(f"^{name_re}$")


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
