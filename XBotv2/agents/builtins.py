"""Built-in Agent definitions shipped with XBot.

These two definitions (``default`` and ``Explorer``) are registered by the
``agents`` plugin before any file-based ``.agents/*.md`` definitions, so a
same-named data-root or workspace Markdown file replaces the built-in (data
root wins over the built-in, workspace wins over the data root).  They keep a
fresh install usable out of the box without shipping Markdown templates.
"""

from __future__ import annotations

import fnmatch

from XBotv2.agents.contracts import (
    AgentDefinition,
    AgentModelPolicy,
    AgentToolPolicy,
)
from XBotv2.permissions.contracts import PermissionPolicy, PermissionRule


def _deny(value: str) -> PermissionRule:
    return PermissionRule(
        tool_pattern=fnmatch.translate(value),
        decision="deny",
    )


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
        model_policy=AgentModelPolicy(temperature=0.1),
        tool_policy=AgentToolPolicy(
            enabled=(
                "read",
                "search",
                "ask_user",
            ),
        ),
        permission_policy=PermissionPolicy(
            rules=(
                _deny("edit"),
                _deny("path"),
                _deny("shell"),
                _deny("*subagent*"),
            ),
            default_decision="allow",
        ),
        prompt=(
            "Explore the workspace, trace behavior, and report evidence with "
            "file references.\nDo not modify files or start other agents."
        ),
    ),
)

__all__ = ["BUILTIN_AGENT_DEFINITIONS"]
