"""Integration tests for SkillsPlugin — discovery, loading, and shell injection."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def skill_workspace(tmp_path):
    """Create a workspace with SKILL.md files for testing."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".git").mkdir()  # Mark as git root

    # Create a valid skill
    skill_dir = ws / ".claude" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("""---
name: test-skill
description: A test skill for integration testing
allowed-tools: Bash(git *)
disallowed-tools: AskUserQuestion
---
# Test Skill

This is a test skill body.
""")

    # Create a skill without frontmatter (invalid)
    invalid_dir = ws / ".claude" / "skills" / "invalid-skill"
    invalid_dir.mkdir(parents=True)
    (invalid_dir / "SKILL.md").write_text("No frontmatter here")

    # Create a skill in .agents path
    agents_dir = ws / ".agents" / "skills" / "agents-skill"
    agents_dir.mkdir(parents=True)
    (agents_dir / "SKILL.md").write_text("""---
name: agents-skill
description: A skill found via .agents path
---
Agents path skill.
""")

    return ws


class TestSkillRegistry:
    def test_discover_finds_skills_in_claude_path(self, skill_workspace):
        from builtin_plugins.skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        skills = reg.list_skills()
        names = {s.name for s in skills}
        assert "test-skill" in names
        assert "agents-skill" in names
        assert "invalid-skill" not in names

    def test_skill_parses_frontmatter(self, skill_workspace):
        from builtin_plugins.skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        skill = reg.load_skill("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill for integration testing"
        assert "git" in str(skill.allowed_tools)
        assert "AskUserQuestion" in str(skill.disallowed_tools)
        assert "test skill body" in skill.content.lower()

    def test_skill_found_via_agents_path(self, skill_workspace):
        from builtin_plugins.skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        skill = reg.load_skill("agents-skill")
        assert skill is not None
        assert "agents path" in skill.content.lower()

    def test_load_skill_returns_none_for_unknown(self, skill_workspace):
        from builtin_plugins.skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        assert reg.load_skill("nonexistent") is None

    def test_skill_registry_respects_name_directory_match(self, tmp_path):
        """Skill name must match the containing directory name."""
        from builtin_plugins.skills.registry import SkillRegistry

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()
        skill_dir = ws / ".claude" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        # Name in frontmatter doesn't match directory
        (skill_dir / "SKILL.md").write_text("""---
name: wrong-name
description: Name mismatch
---
Body
""")

        reg = SkillRegistry()
        reg.discover(ws)
        assert reg.load_skill("wrong-name") is None
        assert reg.load_skill("my-skill") is None


class TestSkillToolAndShellInjection:
    @pytest.mark.asyncio
    async def test_shell_injection_expands_command(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess("hello !`echo world`")
        assert "world" in result

    @pytest.mark.asyncio
    async def test_shell_injection_no_backticks_unchanged(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess("hello echo world")
        assert result == "hello echo world"

    @pytest.mark.asyncio
    async def test_shell_injection_multiple_commands(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess("a !`echo X` and !`echo Y` end")
        assert "X" in result
        assert "Y" in result
        assert "!`" not in result

    @pytest.mark.asyncio
    async def test_load_skill_returns_content(self):
        from builtin_plugins.skills.registry import SkillRegistry
        from builtin_plugins.skills.skill_tool import load_skill

        import tempfile
        ws = Path(tempfile.mkdtemp())
        (ws / ".git").mkdir()
        sd = ws / ".claude" / "skills" / "demo"
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text("""---
name: demo
description: Demo skill
---
Demo content
""")

        reg = SkillRegistry()
        reg.discover(ws)

        result = await load_skill("demo", skill_registry=reg)
        assert "# demo" in result.lower() or "## demo" in result.lower()
        assert "Demo content" in result


class TestSkillPermissionScope:
    def test_allowed_tools_match(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["Bash(git *)"])
        assert scope.check("Bash") is None  # Bash without args doesn't match
        scope.add(allowed=["Bash"])
        assert scope.check("Bash") == "allow"

    def test_disallowed_overrides_allowed(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["Bash"], disallowed=["Bash"])
        assert scope.check("Bash") == "deny"

    def test_clear_resets(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["Bash"])
        assert scope.check("Bash") == "allow"
        scope.clear()
        assert scope.check("Bash") is None
