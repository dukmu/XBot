"""skill tool helpers — shell injection preprocessing for SKILL.md content."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

_SHELL_INJECT_RE = re.compile(r"!`([^`]+)`")


async def load_skill(name: str, *, skill_registry: Any = None) -> str:
    if skill_registry is None:
        return "Error: skills plugin not loaded"
    skill = skill_registry.load_skill(name)
    if skill is None:
        return f"Error: skill '{name}' not found"
    content = await _preprocess(skill.content)
    return f"## {skill.name}\n\n{content}"


async def _preprocess(content: str) -> str:
    commands = [(m.group(0), m.group(1).strip()) for m in _SHELL_INJECT_RE.finditer(content)]
    if not commands:
        return content
    for placeholder, cmd in commands:
        try:
            output = await _run_command(cmd)
        except Exception as exc:
            output = f"[shell injection error: {exc}]"
        content = content.replace(placeholder, output, 1)
    return content


async def _run_command(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.getcwd(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[shell injection timed out: {cmd}]"
    output = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        if err:
            output = f"{output}\n{err}" if output else err
    return output
