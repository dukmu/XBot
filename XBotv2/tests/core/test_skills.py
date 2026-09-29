"""Skill discovery and per-turn permission scope stay plugin-owned."""

import pytest

from XBotv2.application.app import start_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import Tool, ToolCall, ToolDenied, ToolSucceeded
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions import PermissionPolicy, PermissionRule
from XBotv2.skills.permission_scope import SkillPermissionScope, validate_tool_patterns
from XBotv2.skills.registry import SkillRegistry
from XBotv2.skills.skill_tool import load_skill


def _write_skill(root, name="review", frontmatter="", body="Review $ARGUMENTS"):
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review code\n{frontmatter}---\n{body}\n",
        encoding="utf-8",
    )


def test_registry_discovers_valid_project_skill(tmp_path):
    (tmp_path / ".git").mkdir()
    _write_skill(tmp_path)
    registry = SkillRegistry()
    registry.discover(tmp_path)
    skill = registry.load_skill("review")
    assert skill is not None
    assert skill.scope == "project"
    assert skill.content == "Review $ARGUMENTS"


def test_project_skill_precedes_same_named_global_skill(tmp_path):
    workspace = tmp_path / "workspace"
    global_root = tmp_path / "global"
    (workspace / ".git").mkdir(parents=True)
    _write_skill(workspace, body="project")
    global_skill = global_root / "review"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Global\n---\nglobal\n", encoding="utf-8",
    )
    registry = SkillRegistry()
    registry.discover(workspace, global_dirs=[global_root])
    assert registry.load_skill("review").content == "project"


@pytest.mark.parametrize("frontmatter", [
    "disable-model-invocation: 'true'\n",
    "allowed-tools:\n  - shell(git *\n",
    "user-invocable: maybe\n",
])
def test_registry_rejects_malformed_owned_fields(tmp_path, frontmatter):
    (tmp_path / ".git").mkdir()
    _write_skill(tmp_path, frontmatter=frontmatter)
    registry = SkillRegistry()
    registry.discover(tmp_path)
    assert registry.load_skill("review") is None


def test_permission_scope_applies_deny_then_allow_then_restrict():
    scope = SkillPermissionScope()
    scope.add(
        allowed=["read", "shell(git *)"],
        disallowed=["shell(git push*)"],
    )
    assert scope.check("shell", {"command": "git push origin main"}) == "deny"
    assert scope.check("shell", {"command": "git status"}) == "allow"
    assert scope.check("read", {"path": "x"}) == "allow"
    assert scope.check("edit", {"path": "x"}) == "restrict"
    scope.clear()
    assert scope.check("edit") is None


def test_permission_patterns_fail_at_skill_boundary():
    with pytest.raises(ValueError):
        validate_tool_patterns(["shell(git *"])


@pytest.mark.asyncio
async def test_load_skill_expands_arguments_without_global_runtime_state(tmp_path):
    (tmp_path / ".git").mkdir()
    _write_skill(tmp_path, body="all=$ARGUMENTS first=$1 second=$2")
    registry = SkillRegistry()
    registry.discover(tmp_path)
    assert await load_skill(
        "review", arguments="one two", skill_registry=registry,
    ) == "all=one two first=one second=two"


@pytest.mark.asyncio
async def test_shell_injection_requires_explicit_enabled_sandbox(tmp_path):
    (tmp_path / ".git").mkdir()
    _write_skill(tmp_path, body="value=!`pwd`")
    registry = SkillRegistry()
    registry.discover(tmp_path)
    assert "enabled sandbox required" in await load_skill(
        "review", skill_registry=registry,
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
        context.tools.register(
            Tool.from_function(mutate_workspace), cleanup="caller",
        )
        first = await context.tools.execute_all([
            ToolCall(id="load-review", name="review", args={}),
        ])
        second = await context.tools.execute_all([
            ToolCall(id="load-verify", name="verify", args={}),
        ])
        ordinary = await context.tools.execute_all([
            ToolCall(id="mutate", name="mutate_workspace", args={}),
        ])

        assert isinstance(first[0].message.outcome, ToolSucceeded)
        assert isinstance(second[0].message.outcome, ToolSucceeded)
        assert isinstance(ordinary[0].message.outcome, ToolDenied)
    finally:
        await context.destroy()
