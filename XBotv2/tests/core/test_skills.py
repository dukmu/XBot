"""Skill discovery and per-turn permission scope stay plugin-owned."""

from XBotv2.agentloop.api.contracts import TOOLS
import pytest
from XBotv2.application.app import start_application
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import Tool, ToolCall, ToolDenied, ToolSucceeded
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule


def _write_skill(root, name="review", frontmatter="", body="Review $ARGUMENTS"):
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review code\n{frontmatter}---\n{body}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_active_skill_scope_keeps_other_skill_tools_callable(tmp_path):
    async def mutate_workspace() -> str:
        """Mutate the workspace."""
        return "changed"

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    _write_skill(
        workspace,
        name="review",
        frontmatter="allowed-tools:\n  - read\n",
    )
    _write_skill(workspace, name="verify")
    context = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="skill-chaining",
        thread_id="main",
        workspace_root=workspace,
        llm_override=MockLLM(),
    )
    try:
        context.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        context.require(TOOLS).register(
            Tool.from_function(mutate_workspace), cleanup="caller",
        )
        first = await context.require(TOOLS).execute_all([
            ToolCall(id="load-review", name="review", args={}),
        ])
        second = await context.require(TOOLS).execute_all([
            ToolCall(id="load-verify", name="verify", args={}),
        ])
        ordinary = await context.require(TOOLS).execute_all([
            ToolCall(id="mutate", name="mutate_workspace", args={}),
        ])

        assert isinstance(first[0].message.outcome, ToolSucceeded)
        assert isinstance(second[0].message.outcome, ToolSucceeded)
        assert isinstance(ordinary[0].message.outcome, ToolDenied)
    finally:
        await context.destroy()
