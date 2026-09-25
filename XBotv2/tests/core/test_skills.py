"""Skill discovery and per-turn permission scope stay plugin-owned."""

import pytest

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
