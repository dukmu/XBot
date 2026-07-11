"""skill tool helpers — shell injection preprocessing for SKILL.md content."""

from __future__ import annotations

import re
from typing import Any

_SHELL_INJECT_RE = re.compile(r"!`([^`]+)`")


async def load_skill(
    name: str, *, skill_registry: Any = None, sandbox: Any = None
) -> str:
    if skill_registry is None:
        return "Error: skills plugin not loaded"
    skill = skill_registry.load_skill(name)
    if skill is None:
        return f"Error: skill '{name}' not found"
    content = await _preprocess(skill.content, sandbox=sandbox)
    return f"## {skill.name}\n\n{content}"


async def _preprocess(content: str, *, sandbox: Any = None) -> str:
    commands = [(m.group(0), m.group(1).strip()) for m in _SHELL_INJECT_RE.finditer(content)]
    if not commands:
        return content
    for placeholder, cmd in commands:
        try:
            output = await _run_command(cmd, sandbox=sandbox)
        except Exception as exc:
            output = f"[shell injection error: {exc}]"
        content = content.replace(placeholder, output, 1)
    return content


async def _run_command(cmd: str, *, sandbox: Any = None) -> str:
    if sandbox is None or not sandbox.enabled:
        return "[shell injection unavailable: enabled sandbox required]"
    return await sandbox.run_shell(cmd)
