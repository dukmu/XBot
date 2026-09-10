"""Built-in Agent definitions shipped with XBot.

These two definitions (``default`` and ``Explorer``) are registered by the
``agents`` plugin before any file-based ``.agents/*.md`` definitions, so a
same-named data-root or workspace Markdown file replaces the built-in (data
root wins over the built-in, workspace wins over the data root).  They keep a
fresh install usable out of the box without shipping Markdown templates.
"""

from __future__ import annotations

import fnmatch

from XBotv2.agents.contracts import AgentDefinition


def _tool_pattern(value: str) -> dict[str, str]:
    return {"tool": fnmatch.translate(value)}


BUILTIN_AGENT_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        name="default",
        description="General-purpose coding agent",
        mode="all",
    ),
    AgentDefinition(
        name="Explorer",
        description="Read-only workspace exploration and codebase analysis",
        mode="all",
        temperature=0.1,
        tools=(
            "read",
            "search",
            "ask_user",
        ),
        permissions={
            "deny": [
                _tool_pattern("edit"),
                _tool_pattern("path"),
                _tool_pattern("shell"),
                _tool_pattern("*subagent*"),
            ],
        },
        prompt=(
            "Explore the workspace, trace behavior, and report evidence with "
            "file references.\nDo not modify files or start other agents."
        ),
    ),
)

__all__ = ["BUILTIN_AGENT_DEFINITIONS"]
