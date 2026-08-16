"""Integration tests for SkillsPlugin — discovery, loading, and shell injection."""

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.models import RuntimeConfig


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
xbotv2-disallowed-tools: ask_user
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
$ARGUMENTS
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

    model_only_dir = ws / ".agents" / "skills" / "model-only"
    model_only_dir.mkdir(parents=True)
    (model_only_dir / "SKILL.md").write_text("""---
name: model-only
description: A model-only skill
user-invocable: false
---
Model-only content.
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
        from skills.registry import SkillRegistry

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
        from skills.registry import SkillRegistry

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
        from skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        skill = reg.load_skill("agents-skill")
        assert skill is not None
        assert "agents path" in skill.content.lower()

    def test_load_skill_returns_none_for_unknown(self, skill_workspace):
        from skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover(skill_workspace)
        assert reg.load_skill("nonexistent") is None

    def test_skill_registry_respects_name_directory_match(self, tmp_path):
        """Skill name must match the containing directory name."""
        from skills.registry import SkillRegistry

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
    async def test_plugin_unload_resets_runtime_state(self, skill_workspace, state_store):
        from skills.plugin import SkillsPlugin
        from plugin_harness import mount_plugin

        plugin = SkillsPlugin()
        mount_plugin(plugin, state_store)
        plugin._registry.discover(skill_workspace)
        plugin._active_skills.add("test-skill")
        plugin._permission_scope.add(allowed=["shell"], disallowed=[])

        plugin._cleanup_runtime()

        assert plugin.diagnostics() == {
            "status": "ready",
            "skills": 0,
            "active_skills": 0,
        }

    @pytest.mark.asyncio
    async def test_plugin_session_init_is_idempotent(self, skill_workspace, state_store):
        from skills.plugin import SkillsPlugin
        from api import EventContext
        from plugin_harness import mount_plugin

        plugin = SkillsPlugin()
        plugin._registry._scan_global = lambda: None
        mount_plugin(plugin, state_store)
        registry = plugin.ctx.tools.registry
        ctx = EventContext(
            session=SimpleNamespace(workspace_root=str(skill_workspace)),
            config=SimpleNamespace(max_context_tokens=1_000),
        )

        await plugin._on_session_init(ctx)
        first_names = registry.registered_names()
        await plugin._on_session_init(ctx)

        assert registry.registered_names() == first_names
        assert plugin._skill_tools == first_names
        assert "skills:project:manual-only" not in first_names
        assert plugin.ctx.commands.get("manual-only").kind == "prompt"
        model_only = registry.get_registered("skills:project:model-only")
        assert model_only is not None
        assert model_only.model_visible is True
        assert plugin.ctx.commands.get("model-only") is None
        assert plugin.ctx.commands.get("test-skill").kind == "prompt"
        entry = registry.get("skills:project:test-skill")
        assert entry is not None
        assert entry.sandbox_mode == "sandboxed"
        assert plugin._initialized is True
        assert plugin._metadata_budget_chars == 80

        result = await entry.tool.ainvoke({})

        assert result.status == "success"
        assert "test skill body" in result.content.lower()
        assert result.data == {"name": "test-skill", "scope": "project"}
        assert plugin.diagnostics()["active_skills"] == 1
        assert plugin._permission_scope.check("ask_user") == "deny"

    @pytest.mark.asyncio
    async def test_manual_only_skill_requires_explicit_user_invocation(
        self,
        skill_workspace,
    ):
        from skills.plugin import SkillsPlugin
        
        plugin = SkillsPlugin()
        plugin._registry._scan_global = lambda: None
        plugin._registry.discover(skill_workspace)
        manual_result = await plugin._on_before_user_message(
            SimpleNamespace(user_input="/manual-only focus", sandbox=None)
        )

        invocation = manual_result["user_input"]
        root = ET.fromstring(invocation)
        assert invocation.startswith('<skill_invocation name="manual-only"')
        assert root.attrib["name"] == "manual-only"
        assert "Manual skill content." in root.findtext("skill_instructions")
        assert root.findtext("user_arguments").strip() == "focus"

        model_only_result = await plugin._on_before_user_message(
            SimpleNamespace(user_input="/model-only", sandbox=None)
        )
        assert model_only_result["event"]["data"]["code"] == (
            "skill_not_user_invocable"
        )

    @pytest.mark.asyncio
    async def test_manual_only_skill_cannot_be_guessed_as_a_model_tool(
        self,
        skill_workspace,
        state_store,
        temp_workspace,
    ):
        from skills.plugin import SkillsPlugin
        from api import EventContext
        from core.context import ContextBuilder
        from core.engine import Engine
        from llm.mock import MockLLM
        from tools.permissions import PermissionSystem
        from tools.registry import ToolRegistry
        from tools.sandbox import SandboxPolicy
        from plugin_harness import mount_plugin
        from xcore import Context

        plugin = SkillsPlugin()
        plugin._registry._scan_global = lambda: None
        mount_plugin(plugin, state_store)
        await plugin._on_session_init(EventContext(
            session=SimpleNamespace(workspace_root=str(skill_workspace)),
            config=None,
        ))
        engine = Engine(
            llm=MockLLM(responses=[
                {
                    "content": "",
                    "tool_calls": [{
                        "id": "manual",
                        "name": "manual-only",
                        "args": {},
                    }],
                },
                {"content": "done"},
            ]),
            tool_registry=plugin.ctx.tools.registry,
            plugin_ctx=Context(),
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [event async for event in engine.run_turn("load manual skill")]
        tool_event = next(event for event in events if event["type"] == "tool_result")

        assert tool_event["data"]["status"] == "error"
        assert "not registered" in tool_event["data"]["content"].lower()

    @pytest.mark.asyncio
    async def test_skill_schema_budget_preserves_non_skill_tools(self):
        from skills.plugin import SkillsPlugin
        from api import Tool

        plugin = SkillsPlugin()
        plugin._model_skill_names = {"long-skill"}
        plugin._metadata_budget_chars = 14

        async def invoke():
            return None

        ordinary = Tool.from_function(invoke, name="ordinary")
        skill = Tool(
            name="long-skill",
            description="a very long description",
            function=invoke,
            parameters={"type": "object", "properties": {}},
        )
        result = await plugin._on_before_tool_schema(SimpleNamespace(
            model_request={"tools": [ordinary, skill]},
        ))

        assert result["tools"][0] is ordinary
        assert result["tools"][1].name == "long-skill"
        assert result["tools"][1].description == "a ve"

    @pytest.mark.asyncio
    async def test_active_skill_checks_tool_call_arguments(self):
        from skills.plugin import SkillsPlugin
        from api import (
            EventContext,
            ToolAction,
            ToolCall,
        )

        plugin = SkillsPlugin()
        plugin._active_skills.add("git-workflow")
        plugin._permission_scope.add(allowed=["shell(git *)"])

        allowed = await plugin._on_before_tool(
            EventContext(
                tool_call=ToolCall("call_1", "shell", {"command": "git status"}),
            )
        )
        denied = await plugin._on_before_tool(
            EventContext(
                tool_call=ToolCall("call_2", "shell", {"command": "rm -rf build"}),
            )
        )

        assert allowed.action is ToolAction.ALLOW
        assert denied is None

    @pytest.mark.asyncio
    async def test_skill_restrictions_run_before_core_permissions(
        self,
        state_store,
        temp_workspace,
    ):
        from skills.plugin import SkillsPlugin
        from api import Events, Tool
        from core.context import ContextBuilder
        from core.engine import Engine
        from llm.mock import MockLLM
        from tools.permissions import PermissionSystem
        from tools.registry import ToolRegistry
        from tools.sandbox import SandboxPolicy
        from xcore import Context

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
        plugin = SkillsPlugin()
        plugin._active_skills.add("restricted")
        plugin._permission_scope.add(allowed=["echo", "runner(git *)"])
        plugin_ctx = Context()
        plugin_ctx.on(Events.BEFORE_TOOL_CALL, plugin._on_before_tool)
        permissions = PermissionSystem(default_decision="ask")
        permissions.add_rule("deny", {"tool": "echo"})
        llm = MockLLM(responses=[
            {
                "content": "try both",
                "tool_calls": [
                    {
                        "id": "core_denied",
                        "name": "echo",
                        "args": {"message": "hi"},
                    },
                    {
                        "id": "allowed_by_skill",
                        "name": "runner",
                        "args": {"command": "git status"},
                    },
                    {
                        "id": "normal_permission",
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
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=permissions,
            config=RuntimeConfig(),
        )

        _ = [event async for event in engine.run_turn("test restrictions")]

        results = {
            message.tool_call_id: message
            for message in engine.messages
            if message.role == "tool"
        }
        assert invoked == [("runner", "git status")]
        assert "Permission denied" in results["core_denied"].content
        assert results["allowed_by_skill"].status == "success"
        assert "approval required" in results["normal_permission"].content

    @pytest.mark.asyncio
    async def test_plugin_session_init_rolls_back_partial_registration(
        self, skill_workspace, state_store
    ):
        from skills.plugin import SkillsPlugin
        from api import EventContext, Tool
        from plugin_harness import mount_plugin

        def existing_tool() -> str:
            return "existing"

        plugin = SkillsPlugin()
        plugin._registry._scan_global = lambda: None
        mount_plugin(plugin, state_store)
        registry = plugin.ctx.tools.registry
        collision_name = registry.register(
            Tool.from_function(existing_tool, name="test-skill"),
            namespace="skills:project",
        )
        ctx = EventContext(
            session=SimpleNamespace(workspace_root=str(skill_workspace)),
        )

        with pytest.raises(ValueError, match="already registered"):
            await plugin._on_session_init(ctx)

        assert registry.registered_names() == [collision_name]
        assert plugin._skill_tools == []
        assert plugin._initialized is False
        assert plugin.diagnostics()["skills"] == 0


class TestSkillToolAndShellInjection:
    class FakeSandbox:
        enabled = True

        async def run_shell(self, command):
            return command.removeprefix("echo ")

    @pytest.mark.asyncio
    async def test_shell_injection_expands_command(self):
        from skills.skill_tool import _preprocess

        result = await _preprocess(
            "hello !`echo world`", sandbox=self.FakeSandbox()
        )
        assert "world" in result

    @pytest.mark.asyncio
    async def test_shell_injection_no_backticks_unchanged(self):
        from skills.skill_tool import _preprocess

        result = await _preprocess("hello echo world")
        assert result == "hello echo world"

    @pytest.mark.asyncio
    async def test_shell_injection_multiple_commands(self):
        from skills.skill_tool import _preprocess

        result = await _preprocess(
            "a !`echo X` and !`echo Y` end", sandbox=self.FakeSandbox()
        )
        assert "X" in result
        assert "Y" in result
        assert "!`" not in result

    @pytest.mark.asyncio
    async def test_shell_injection_without_sandbox_does_not_execute(self):
        from skills.skill_tool import _preprocess

        result = await _preprocess("hello !`echo unsafe`")
        assert result == "hello [shell injection unavailable: enabled sandbox required]"

    @pytest.mark.asyncio
    async def test_load_skill_returns_content(self):
        from skills.registry import SkillRegistry
        from skills.skill_tool import load_skill

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
        assert result == "Demo content"


class TestSkillPermissionScope:
    def test_allowed_tools_match(self):
        from skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["shell(git *)"])
        assert scope.check("shell", {"command": "git status"}) == "allow"
        assert scope.check("shell", {"command": "rm -rf build"}) is None
        assert scope.check("read_file", {"path": "README.md"}) is None

    def test_disallowed_overrides_allowed(self):
        from skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["shell"], disallowed=["shell(git push *)"])
        assert scope.check("shell", {"command": "git status"}) == "allow"
        assert scope.check("shell", {"command": "git push origin main"}) == "deny"

    def test_disallowed_tools_do_not_create_an_allowlist(self):
        from skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(disallowed=["shell(git push *)"])
        assert scope.check("shell", {"command": "git status"}) is None
        assert scope.check("shell", {"command": "git push origin main"}) == "deny"

    def test_clear_resets(self):
        from skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()
        scope.add(allowed=["shell"])
        assert scope.check("shell") == "allow"
        scope.clear()
        assert scope.check("shell") is None

    def test_invalid_update_does_not_leave_partial_rules(self):
        from skills.permission_scope import SkillPermissionScope

        scope = SkillPermissionScope()

        with pytest.raises(ValueError, match="invalid tool permission pattern"):
            scope.add(allowed=["shell", "filesystem_read("])

        assert scope.check("shell") is None
