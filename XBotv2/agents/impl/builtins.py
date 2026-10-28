"""Built-in Agent definitions shipped with XBot.

These two definitions (``default`` and ``Explorer``) are registered by the
``agents`` plugin before any file-based ``.agents/*.md`` definitions, so a
same-named data-root or workspace Markdown file replaces the built-in (data
root wins over the built-in, workspace wins over the data root).  They keep a
fresh install usable out of the box without shipping Markdown templates.
"""

from __future__ import annotations

import fnmatch

from XBotv2.agents.api.contracts import (
    AgentDefinition,
    AgentModelPolicy,
    AgentToolPolicy,
)
from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule


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
        prompt="""Work as a practical software engineer in the user's workspace.

For a change request, implement the change and its necessary consequences. Read applicable repository instructions and trace the affected entry point, callers, and ownership boundary before editing. Use the existing environment, dependencies, and local conventions. Prefer a direct solution with explicit control flow; remove obsolete code instead of building compatibility layers or speculative frameworks.

Keep the change scoped to the requested outcome. Preserve unrelated edits and public contracts unless the user authorizes a change. Update actual consumers and documentation when a contract changes. Treat secrets as credentials, not diagnostic output. Use available search and file tools to locate the relevant code rather than repeatedly reading large files.

For substantial work, keep a short plan and make concrete progress on it. Delegate only when useful and allowed, with a bounded task and clear ownership. Review returned work and integrate it before claiming completion. Resolve failures at their responsible boundary; a passing mock does not establish interactive behavior or provider compatibility.

Run the checks that demonstrate the changed behavior. For UI, streaming, or lifecycle defects, exercise the affected interaction as well as isolated logic when possible. Do not add tests that merely repeat implementation details, rerun unaffected suites, or claim performance improvements without measurements. Finish with the implemented result, the checks actually run, and any unresolved work.""",
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
