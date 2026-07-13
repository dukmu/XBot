"""Integration tests for SkillsPlugin — discovery, loading, and shell injection."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

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
allowed-tools: shell(git *)
disallowed-tools: ask_user
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

    manual_dir = ws / ".agents" / "skills" / "manual-only"
    manual_dir.mkdir(parents=True)
    (manual_dir / "SKILL.md").write_text("""---
name: manual-only
description: A manually invoked skill
disable-model-invocation: true
---
Manual skill content.
""")

    invalid_flag_dir = ws / ".agents" / "skills" / "invalid-flag"
    invalid_flag_dir.mkdir(parents=True)
    (invalid_flag_dir / "SKILL.md").write_text("""---
name: invalid-flag
description: Invalid manual-only flag
disable-model-invocation: "true"
---
Invalid flag content.
""")

    invalid_permissions_dir = ws / ".agents" / "skills" / "invalid-permissions"
    invalid_permissions_dir.mkdir(parents=True)
    (invalid_permissions_dir / "SKILL.md").write_text("""---
name: invalid-permissions
description: Invalid permission pattern
allowed-tools:
  - shell(git *
---
Invalid permission content.
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
        assert "invalid-flag" not in names
        assert "invalid-permissions" not in names

    def test_skill_parses_frontmatter(self, skill_workspace):
        from builtin_plugins.skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        skill = reg.load_skill("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill for integration testing"
        assert "git" in str(skill.allowed_tools)
        assert "ask_user" in str(skill.disallowed_tools)
        assert "test skill body" in skill.content.lower()

        manual = reg.load_skill("manual-only")
        assert manual is not None
        assert manual.disable_model_invocation is True

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

    @pytest.mark.asyncio
    async def test_plugin_unload_resets_runtime_state(self, skill_workspace):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import PluginManifest

        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._registry.discover(skill_workspace)
        plugin._active_skills.add("test-skill")
        plugin._permission_scope.add(allowed=["shell"], disallowed=[])

        await plugin.on_unload()

        assert plugin.diagnostics() == {
            "status": "ready",
            "skills": 0,
            "active_skills": 0,
        }

    @pytest.mark.asyncio
    async def test_plugin_session_init_is_idempotent(self, skill_workspace):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import PluginManifest
        from xbotv2.plugin.loader import _RuntimePluginContext
        from xbotv2.tools.registry import ToolRegistry

        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._registry._scan_global = lambda: None
        registry = ToolRegistry()
        owned_names: list[str] = []
        runtime = _RuntimePluginContext("skills", registry, owned_names)
        ctx = SimpleNamespace(
            plugin_runtime=runtime,
            session=SimpleNamespace(workspace_root=str(skill_workspace)),
        )

        await plugin._on_session_init(ctx)
        first_names = registry.registered_names()
        await plugin._on_session_init(ctx)

        assert registry.registered_names() == first_names
        assert owned_names == first_names
        assert "skills:project:manual-only" not in first_names
        entry = registry.get("skills:project:test-skill")
        assert entry is not None
        assert entry.tool.description == "A test skill for integration testing"
        assert entry.sandbox_mode == "sandboxed"
        assert plugin._initialized is True

        content = await entry.tool.ainvoke({})

        assert "test skill body" in content.lower()
        assert plugin.diagnostics()["active_skills"] == 1
        assert plugin._permission_scope.check("ask_user") == "deny"

    @pytest.mark.asyncio
    async def test_manual_only_skill_requires_explicit_user_invocation(
        self,
        skill_workspace,
    ):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import PluginManifest

        class SetupContext:
            def __init__(self):
                self.skill_tool = None

            def register_hook(self, stage, callback):
                pass

            def register_tool(self, tool, options=None):
                self.skill_tool = tool
                return "plugin:skills:skill"

        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._registry._scan_global = lambda: None
        plugin._registry.discover(skill_workspace)
        setup = SetupContext()
        plugin.setup(setup)

        model_result = await setup.skill_tool.ainvoke({"name": "manual-only"})
        manual_result = await plugin._on_before_user_message(
            SimpleNamespace(user_input="/manual-only focus", sandbox=None)
        )

        assert model_result == (
            "Error: skill 'manual-only' requires explicit /manual-only invocation"
        )
        assert manual_result["user_input"].startswith("## manual-only")
        assert "Manual skill content." in manual_result["user_input"]
        assert "## Instructions\nfocus" in manual_result["user_input"]

    @pytest.mark.asyncio
    async def test_active_skill_context_order_is_stable(self):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import Message, PluginManifest

        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._active_skills.update({"zeta", "alpha"})
        ctx = SimpleNamespace(
            context_messages=[Message(role="system", content="base")]
        )

        result = await plugin._on_after_context(ctx)
        content = result["context_messages"][1].content

        assert content.index("### alpha") < content.index("### zeta")

    @pytest.mark.asyncio
    async def test_active_skill_checks_tool_call_arguments(self):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import (
            HookAction,
            HookContext,
            HookStage,
            PluginManifest,
            ToolCall,
        )

        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._active_skills.add("git-workflow")
        plugin._permission_scope.add(allowed=["shell(git *)"])

        allowed = await plugin._on_before_tool(
            HookContext(
                stage=HookStage.BEFORE_TOOL_CALL,
                tool_call=ToolCall("call_1", "shell", {"command": "git status"}),
            )
        )
        denied = await plugin._on_before_tool(
            HookContext(
                stage=HookStage.BEFORE_TOOL_CALL,
                tool_call=ToolCall("call_2", "shell", {"command": "rm -rf build"}),
            )
        )

        assert allowed is None
        assert denied.action is HookAction.DENY
        assert "not permitted" in denied.reason

    @pytest.mark.asyncio
    async def test_skill_restrictions_run_before_core_permissions(
        self,
        state_store,
        temp_workspace,
    ):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import HookStage, PluginManifest, Tool
        from xbotv2.core.context import ContextBuilder
        from xbotv2.core.engine import Engine
        from xbotv2.hooks.manager import HookManager
        from xbotv2.llm.mock import MockLLM
        from xbotv2.tools.permissions import PermissionSystem
        from xbotv2.tools.registry import ToolRegistry
        from xbotv2.tools.sandbox import SandboxPolicy

        invoked = []

        def echo(message: str) -> str:
            invoked.append(("echo", message))
            return message

        def runner(command: str) -> str:
            invoked.append(("runner", command))
            return command

        registry = ToolRegistry()
        registry.register(Tool.from_function(echo), sandbox_mode="host")
        registry.register(Tool.from_function(runner), sandbox_mode="host")
        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._active_skills.add("restricted")
        plugin._permission_scope.add(allowed=["echo", "runner(git *)"])
        hooks = HookManager()
        hooks.register(HookStage.BEFORE_TOOL_CALL, plugin._on_before_tool)
        permissions = PermissionSystem(default_decision="allow")
        permissions.add_rule("deny", {"tool": "echo"})
        llm = MockLLM(responses=[
            {
                "content": "try both",
                "tool_calls": [
                    {
                        "id": "allowed_by_skill",
                        "name": "echo",
                        "args": {"message": "hi"},
                    },
                    {
                        "id": "denied_by_skill",
                        "name": "runner",
                        "args": {"command": "rm build"},
                    },
                ],
            },
            {"content": "done"},
        ])
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hooks,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=permissions,
            config=None,
        )

        _ = [event async for event in engine.run_turn("test restrictions")]

        results = {
            message.tool_call_id: message
            for message in engine.messages
            if message.role == "tool"
        }
        assert invoked == []
        assert "Permission denied" in results["allowed_by_skill"].content
        assert "not permitted by active skill" in results["denied_by_skill"].content

    @pytest.mark.asyncio
    async def test_plugin_session_init_rolls_back_partial_registration(
        self, skill_workspace
    ):
        from builtin_plugins.skills.plugin import SkillsPlugin
        from xbotv2.api import PluginManifest, Tool
        from xbotv2.plugin.loader import _RuntimePluginContext
        from xbotv2.tools.registry import ToolRegistry

        def existing_tool() -> str:
            return "existing"

        plugin = SkillsPlugin(PluginManifest(name="skills", version="1"), store=None)
        plugin._registry._scan_global = lambda: None
        registry = ToolRegistry()
        collision_name = registry.register(
            Tool.from_function(existing_tool, name="test-skill"),
            namespace="skills:project",
        )
        owned_names: list[str] = []
        runtime = _RuntimePluginContext("skills", registry, owned_names)
        ctx = SimpleNamespace(
            plugin_runtime=runtime,
            session=SimpleNamespace(workspace_root=str(skill_workspace)),
        )

        with pytest.raises(ValueError, match="already registered"):
            await plugin._on_session_init(ctx)

        assert registry.registered_names() == [collision_name]
        assert owned_names == []
        assert plugin._initialized is False
        assert plugin.diagnostics()["skills"] == 0


class TestSkillToolAndShellInjection:
    class FakeSandbox:
        enabled = True

        async def run_shell(self, command):
            return command.removeprefix("echo ")

    @pytest.mark.asyncio
    async def test_shell_injection_expands_command(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess(
            "hello !`echo world`", sandbox=self.FakeSandbox()
        )
        assert "world" in result

    @pytest.mark.asyncio
    async def test_shell_injection_no_backticks_unchanged(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess("hello echo world")
        assert result == "hello echo world"

    @pytest.mark.asyncio
    async def test_shell_injection_multiple_commands(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess(
            "a !`echo X` and !`echo Y` end", sandbox=self.FakeSandbox()
        )
        assert "X" in result
        assert "Y" in result
        assert "!`" not in result

    @pytest.mark.asyncio
    async def test_shell_injection_without_sandbox_does_not_execute(self):
        from builtin_plugins.skills.skill_tool import _preprocess

        result = await _preprocess("hello !`echo unsafe`")
        assert result == "hello [shell injection unavailable: enabled sandbox required]"

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
        scope.add(allowed=["shell(git *)"])
        assert scope.check("shell", {"command": "git status"}) == "allow"
        assert scope.check("shell", {"command": "rm -rf build"}) == "deny"
        assert scope.check("read_file", {"path": "README.md"}) == "deny"

    def test_disallowed_overrides_allowed(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["shell"], disallowed=["shell(git push *)"])
        assert scope.check("shell", {"command": "git status"}) == "allow"
        assert scope.check("shell", {"command": "git push origin main"}) == "deny"

    def test_disallowed_tools_do_not_create_an_allowlist(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(disallowed=["shell(git push *)"])
        assert scope.check("shell", {"command": "git status"}) is None
        assert scope.check("shell", {"command": "git push origin main"}) == "deny"

    def test_clear_resets(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["shell"])
        assert scope.check("shell") == "allow"
        scope.clear()
        assert scope.check("shell") is None

    def test_invalid_update_does_not_leave_partial_rules(self):
        from builtin_plugins.skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()

        with pytest.raises(ValueError, match="invalid tool permission pattern"):
            scope.add(allowed=["shell", "filesystem_read("])

        assert scope.check("shell") is None
