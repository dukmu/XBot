"""Tests for bootstrap with explicit no-plugin or temporary-plugin modes."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from xbotv2.api.paths import RuntimePaths
import yaml

from xbotv2.core.bootstrap import _resolve_plugin_dirs, bootstrap
from xbotv2.llm.mock import MockLLM


class TestBootstrapBasics:
    """Minimal bootstrap without plugins."""

    @pytest.mark.asyncio
    async def test_bootstrap_creates_engine(self, temp_data_dir):
        """Bootstrap returns a working engine."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "Hello!"}]),
        )
        assert engine is not None
        assert engine.turn_count == 0

    @pytest.mark.asyncio
    async def test_noninteractive_bootstrap_hides_blocking_tools(
        self, temp_data_dir
    ):
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="noninteractive",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
            interactive=False,
        )

        names = set(engine.tool_registry.names())
        assert "send_message" in names
        assert "ask_user" not in names
        assert "request_permission" not in names

    @pytest.mark.asyncio
    async def test_bootstrap_applies_system_tool_result_cache_limits(
        self, temp_data_dir, temp_workspace, monkeypatch
    ):
        (temp_data_dir / "config" / "system.yaml").write_text(
            "tool_result_max_inline_chars: 2048\n"
            "tool_result_preview_chars: 512\n",
            encoding="utf-8",
        )
        captured = {}

        def cache_hook(_state_store, **options):
            captured.update(options)

            async def apply(_ctx):
                return None

            return apply

        monkeypatch.setattr("xbotv2.core.bootstrap.make_tool_result_cache_hook", cache_hook)

        await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="cache-config",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert captured == {"max_inline_chars": 2048, "preview_chars": 512}

    @pytest.mark.asyncio
    async def test_bootstrap_rejects_unknown_provider(self, temp_data_dir):
        with pytest.raises(ValueError, match="Unknown provider config: typo"):
            await bootstrap(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                provider_name="typo",
                session_id="unknown-provider",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[]),
            )

    def test_cli_reports_unknown_provider_without_traceback(
        self,
        temp_data_dir,
        monkeypatch,
        capsys,
    ):
        from xbotv2.__main__ import main

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "xbotv2",
                "--mode",
                "once",
                "--data-dir",
                str(temp_data_dir),
                "--provider",
                "typo",
                "prompt",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 2
        assert captured.out == ""
        assert captured.err == (
            "Error: Unknown provider config: typo. No providers are configured.\n"
        )
        assert "Traceback" not in captured.err

    @pytest.mark.asyncio
    async def test_session_init_failure_unloads_runtime_plugin_resources(
        self,
        temp_data_dir,
        tmp_path,
    ):
        import sys

        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "init_fail"
        plugin_dir.mkdir(parents=True)
        unload_marker = tmp_path / "unloaded.txt"
        (plugin_dir / "__init__.py").write_text(
            f"""
from pathlib import Path
from xbotv2.api import HookStage, PluginBase, Tool, ToolRegistrationOptions

def runtime_tool() -> str:
    return "ok"

class InitFailPlugin(PluginBase):
    def setup(self, ctx):
        ctx.register_hook(HookStage.ON_SESSION_INIT, self.on_session_init)

    async def on_session_init(self, ctx):
        ctx.plugin_runtime.register_tool(
            Tool.from_function(runtime_tool),
            options=ToolRegistrationOptions(namespace="plugin:init-fail"),
        )
        raise RuntimeError("session init failed")

    async def on_unload(self):
        Path({str(unload_marker)!r}).write_text("unloaded", encoding="utf-8")
""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "init_fail", "version": "1.0.0"}),
            encoding="utf-8",
        )

        with pytest.raises(
            BaseExceptionGroup,
            match="Hook failures for stage on_session_init",
        ) as exc_info:
            await bootstrap(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                session_id="init-fail",
                thread_id="t",
                plugin_dirs=[plugins_root],
                llm_override=MockLLM(responses=[]),
            )

        assert "session init failed" in repr(exc_info.value.exceptions[0])
        assert unload_marker.read_text(encoding="utf-8") == "unloaded"
        assert str(plugins_root) not in sys.path

    @pytest.mark.asyncio
    async def test_normal_session_close_unloads_runtime_plugin_resources(
        self,
        temp_data_dir,
        tmp_path,
    ):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "normal_close"
        plugin_dir.mkdir(parents=True)
        lifecycle_log = tmp_path / "lifecycle.txt"
        (plugin_dir / "__init__.py").write_text(
            f"""
from pathlib import Path
from xbotv2.api import HookStage, PluginBase, Tool, ToolRegistrationOptions

LOG = Path({str(lifecycle_log)!r})

def runtime_tool() -> str:
    return "ok"

class NormalClosePlugin(PluginBase):
    def setup(self, ctx):
        ctx.register_hook(HookStage.ON_SESSION_INIT, self.on_session_init)
        ctx.register_hook(HookStage.ON_SESSION_CLOSE, self.on_session_close)

    async def on_session_init(self, ctx):
        ctx.plugin_runtime.register_tool(
            Tool.from_function(runtime_tool),
            options=ToolRegistrationOptions(namespace="plugin:normal-close"),
        )

    async def on_session_close(self, ctx):
        del ctx
        LOG.write_text("close\\n", encoding="utf-8")

    async def on_unload(self):
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write("unload\\n")
""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "normal_close", "version": "1.0.0"}),
            encoding="utf-8",
        )

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="normal-close",
            thread_id="t",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[]),
        )
        loader = engine.plugin_loader
        tool_name = "plugin:normal-close:runtime_tool"
        assert loader is not None
        assert engine.tool_registry.registered(tool_name)

        await engine.start_session()
        await engine.close_session()

        assert lifecycle_log.read_text(encoding="utf-8").splitlines() == [
            "close",
            "unload",
        ]
        assert not engine.tool_registry.registered(tool_name)
        assert loader.loaded_plugins == []
        assert engine.plugin_loader is None

    @pytest.mark.asyncio
    async def test_bootstrap_rejects_path_like_identifiers(self, temp_data_dir, tmp_path):
        """Runtime identifiers cannot escape the configured data directory."""
        with pytest.raises(ValueError, match="session_id"):
            await bootstrap(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                session_id="../escape",
                thread_id="test-thread",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[]),
            )

        assert not (tmp_path / "escape").exists()

    @pytest.mark.asyncio
    async def test_bootstrap_registers_core_tools(self, temp_data_dir):
        """Core base tools are always registered."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )
        tool_names = set(engine.tool_registry.names())
        assert {
            "shell",
            "filesystem_read",
            "ask_user",
            "request_permission",
            "list_tasks",
        } <= tool_names
        assert "ask" not in tool_names

    @pytest.mark.asyncio
    async def test_shipped_tool_filter_keeps_client_interaction_tools(
        self,
        temp_data_dir,
    ):
        shipped = Path("XBotv2/data/config/system.yaml")
        (temp_data_dir / "config" / "system.yaml").write_text(
            shipped.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="default-tools",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert {
            "ask_user",
            "filesystem_mkdir",
            "list_tasks",
            "request_permission",
        } <= set(engine.tool_registry.names())
        assert "filesystem" not in engine.config.tools

    @pytest.mark.asyncio
    async def test_bootstrap_tool_filter_limits_visible_tools(self, temp_data_dir):
        """System tool selectors restrict tools passed to the model."""
        system = temp_data_dir / "config" / "system.yaml"
        system.write_text("tools:\n  - filesystem_read\n", encoding="utf-8")

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert engine.tool_registry.names() == ["filesystem_read"]
        assert [tool.name for tool in engine.tool_registry.get_all()] == ["filesystem_read"]

    @pytest.mark.asyncio
    async def test_bootstrap_unknown_tool_filter_silently_ignored(self, temp_data_dir):
        """Unknown tool selectors are silently ignored (no tools enabled)."""
        system = temp_data_dir / "config" / "system.yaml"
        system.write_text("tools:\n  - no_such_tool\n", encoding="utf-8")

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )
        assert len(engine.tool_registry) == 0

    @pytest.mark.asyncio
    async def test_bootstrap_tool_filter_can_select_plugin_tools(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """System tool selectors are applied after plugin tools load."""
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "simple"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text("")
        (plugin_dir / "tools.py").write_text(
            """
from xbotv2.api.tools import Tool

def _plugin_tool() -> str:
    \"\"\"Plugin tool.\"\"\"
    return "plugin ok"

plugin_tool = Tool.from_function(_plugin_tool, name="plugin_tool")
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({
                "name": "simple",
                "version": "1.0.0",
                "tools": [{"handler": "simple.tools:plugin_tool"}],
            })
        )
        monkeypatch.syspath_prepend(str(plugins_root))

        system = temp_data_dir / "config" / "system.yaml"
        system.write_text("tools:\n  - plugin_tool\n", encoding="utf-8")

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[]),
        )

        assert engine.tool_registry.names() == ["plugin_tool"]
        assert [tool.name for tool in engine.tool_registry.get_all()] == ["plugin_tool"]
        assert engine.tool_registry.get("filesystem_read") is None

    @pytest.mark.asyncio
    async def test_bootstrap_registers_system_hooks(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """System-declared hooks are resolved and registered."""
        hook_dir = tmp_path / "hook_modules"
        hook_dir.mkdir()
        (hook_dir / "test_personality_hooks.py").write_text(
            """
async def before_user_message(ctx):
    return {"user_input": ctx.user_input + " from hook"}
"""
        )
        monkeypatch.syspath_prepend(str(hook_dir))

        system = temp_data_dir / "config" / "system.yaml"
        system.write_text(
            """
hooks:
  - stage: before_user_message_accept
    target: test_personality_hooks:before_user_message
""",
            encoding="utf-8",
        )

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )

        events = [e async for e in engine.run_turn("hello")]

        assert events[-1]["type"] == "turn_finished"
        assert engine.messages[0].content == "hello from hook"

    @pytest.mark.asyncio
    async def test_bootstrap_invalid_system_hook_raises(self, temp_data_dir):
        """Broken system hook declarations fail loudly."""
        system = temp_data_dir / "config" / "system.yaml"
        system.write_text(
            """
hooks:
  - stage: on_turn_start
    target: missing_module:nope
""",
            encoding="utf-8",
        )

        with pytest.raises(ModuleNotFoundError):
            await bootstrap(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                session_id="test-session",
                thread_id="test-thread",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[]),
            )

    @pytest.mark.asyncio
    async def test_workspace_hook_script_loads_relative_to_xbot_directory(
        self, temp_data_dir, temp_workspace
    ):
        config_dir = temp_workspace / ".xbot"
        (config_dir / "hooks").mkdir(parents=True)
        (config_dir / "hooks" / "rewrite.py").write_text(
            "async def rewrite(ctx):\n"
            "    return {'user_input': ctx.user_input + ' from workspace'}\n",
            encoding="utf-8",
        )
        (config_dir / "hooks.yaml").write_text(
            "hooks:\n"
            "  - stage: before_user_message_accept\n"
            "    target: hooks/rewrite.py:rewrite\n",
            encoding="utf-8",
        )
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )

        _ = [event async for event in engine.run_turn("hello")]

        assert engine.messages[0].content == "hello from workspace"

    @pytest.mark.asyncio
    async def test_bootstrap_passes_external_plugin_configs(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """External bootstrap plugin_configs reach plugin on_load."""
        plugin_root = tmp_path / "plugins"
        plugin_dir = plugin_root / "configured"
        plugin_dir.mkdir(parents=True)
        output_path = tmp_path / "received.json"
        (plugin_dir / "plugin.yaml").write_text(
            """
name: configured
version: 0.1.0
"""
        )
        (plugin_dir / "__init__.py").write_text(
            f"""
import json
from xbotv2.api.plugins import PluginBase

class ConfiguredPlugin(PluginBase):
    async def on_load(self, config=None):
        with open({str(output_path)!r}, "w", encoding="utf-8") as fh:
            json.dump(config or {{}}, fh, sort_keys=True)
"""
        )
        monkeypatch.syspath_prepend(str(plugin_dir))

        await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugin_root],
            plugin_configs={"configured": {"value": 42}},
            llm_override=MockLLM(responses=[]),
        )

        assert json.loads(output_path.read_text(encoding="utf-8")) == {"value": 42}

    @pytest.mark.asyncio
    async def test_bootstrap_engine_runs_turn(self, temp_data_dir, temp_workspace):
        """Engine from bootstrap can run a turn."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "Hello from bootstrap!"}]),
        )
        # Override workspace for the sandbox
        engine.sandbox_policy.workspace_root = temp_workspace

        events = [e async for e in engine.run_turn("hi")]
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 1
        assert "Hello from bootstrap!" in assistant_events[0]["data"]["content"]

    @pytest.mark.asyncio
    async def test_bootstrap_creates_state(self, temp_data_dir):
        """Bootstrap creates the state store with messages file."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )
        assert engine.state_store.session_id == "test-session"
        assert engine.state_store.messages_path.exists()

    @pytest.mark.asyncio
    async def test_bootstrap_includes_workspace_agents_md(self, temp_data_dir, temp_workspace):
        """The default workspace plugin injects AGENTS.md into model context."""
        (temp_workspace / "AGENTS.md").write_text(
            "Workspace instruction path:\n"
            "```var\n"
            "${workspace}\n"
            "```\n"
            "Keep ${workspace} and ${UNRELATED} literal.",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [e async for e in engine.run_turn("hello")]

        system = llm.get_call_messages(0)[0]
        root = ET.fromstring(system.content)
        workspace = next(
            element
            for element in root.findall("plugin_instruction")
            if element.attrib["name"] == "workspace_instructions"
        )
        assert workspace.attrib["source"] == "AGENTS.md"
        assert workspace.text.strip() == (
            f"Workspace instruction path:\n{temp_workspace}\n"
            "Keep ${workspace} and ${UNRELATED} literal."
        )

    @pytest.mark.asyncio
    async def test_workspace_agents_md_reloads_between_model_requests(
        self, temp_data_dir, temp_workspace
    ):
        instructions = temp_workspace / "AGENTS.md"
        instructions.write_text("Workspace rule version one.", encoding="utf-8")
        llm = MockLLM(responses=[
            {"content": "first"},
            {"content": "second"},
            {"content": "third"},
        ])
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="dynamic-instructions",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in engine.run_turn("first turn")]
        instructions.write_text("Workspace rule version two.", encoding="utf-8")
        _ = [event async for event in engine.run_turn("second turn")]
        instructions.unlink()
        _ = [event async for event in engine.run_turn("third turn")]

        first_system = str(llm.get_call_messages(0)[0].content)
        second_system = str(llm.get_call_messages(1)[0].content)
        third_system = str(llm.get_call_messages(2)[0].content)
        assert "Workspace rule version one." in first_system
        assert "Workspace rule version two." not in first_system
        assert "Workspace rule version two." in second_system
        assert "Workspace rule version one." not in second_system
        assert "Workspace rule version one." not in third_system
        assert "Workspace rule version two." not in third_system

    @pytest.mark.asyncio
    async def test_bootstrap_uses_configured_human_identity(
        self, temp_data_dir, temp_workspace
    ):
        (temp_data_dir / "config" / "user.yaml").write_text(
            "user_id: human-7\nuser_name: Ada\nplatform: tui\n",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="identity",
            thread_id="main",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        _ = [event async for event in engine.run_turn("hello")]

        root = ET.fromstring(llm.get_call_messages(0)[0].content)
        runtime = root.findtext("runtime_environment") or ""
        assert "Human: Ada (human-7)" in runtime
        assert f"- workspace: {temp_workspace}" in runtime
        assert "- tool_results: session/artifacts/tool_results/ (read-only)" in runtime

    @pytest.mark.asyncio
    async def test_bootstrap_separates_configured_and_agent_instructions(
        self, temp_data_dir, temp_workspace
    ):
        (temp_data_dir / "config" / "system.yaml").write_text(
            "instructions: Configured rule.\n",
            encoding="utf-8",
        )
        agents_dir = temp_data_dir / ".agents"
        agents_dir.mkdir()
        (agents_dir / "default.md").write_text(
            "---\ndescription: Default Agent\nmode: all\n---\nAgent workflow.",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="instruction-sources",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in engine.run_turn("hello")]

        root = ET.fromstring(llm.get_call_messages(0)[0].content)
        assert root.findtext("developer_instructions").strip() == "Configured rule."
        assert root.findtext("agent_instructions").strip() == "Agent workflow."

    @pytest.mark.asyncio
    async def test_workspace_can_disable_agents_md_plugin(
        self, temp_data_dir, temp_workspace
    ):
        (temp_workspace / "AGENTS.md").write_text("must not appear", encoding="utf-8")
        (temp_workspace / ".xbot").mkdir()
        (temp_workspace / ".xbot" / "plugins.yaml").write_text(
            "plugins:\n  workspace_instructions:\n    enabled: false\n",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in engine.run_turn("hello")]

        prompt = "\n".join(str(msg.content) for msg in llm.get_call_messages(0))
        assert "must not appear" not in prompt

    @pytest.mark.asyncio
    async def test_shell_tool_runs_in_workspace_root(self, temp_data_dir, temp_workspace):
        """Shell tool defaults cwd to the attached workspace root."""
        (temp_data_dir / "config" / "permissions.yaml").write_text(
            "allow:\n  - tool: shell\n",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[
            {
                "content": "checking cwd",
                "tool_calls": [
                    {"name": "shell", "args": {"command": "pwd"}, "id": "call_pwd"},
                ],
            },
            {"content": "done"},
        ])
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        events = [e async for e in engine.run_turn("where are you?")]

        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert str(temp_workspace) in tool_result["data"]["content"]

    @pytest.mark.asyncio
    async def test_bootstrap_default_session_id_is_generated(self, temp_data_dir):
        """Omitting session_id creates a fresh generated session instead of default."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        state = engine.state_store.read_state()
        assert state["session_id"] != "default"
        assert "-" in state["session_id"]
        assert (
            temp_data_dir
            / "sessions"
            / state["session_id"]
            / "threads"
            / "test-thread"
            / "state"
        ).exists()

    @pytest.mark.asyncio
    async def test_system_json_policy_files_are_ignored(self, temp_data_dir):
        """System policy has YAML sources of truth."""
        (temp_data_dir / "config" / "permissions.yaml").write_text(
            "allow:\n  - tool: filesystem_read\n",
            encoding="utf-8",
        )
        (temp_data_dir / "config" / "sandbox.yaml").write_text(
            "enabled: true\n",
            encoding="utf-8",
        )
        (temp_data_dir / "config" / "permissions.json").write_text(
            '{"deny": [{"tool": "filesystem_read"}]}'
        )
        (temp_data_dir / "config" / "sandbox.json").write_text(
            '{"enabled": false}'
        )

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert engine.permission_system.check("filesystem_read", {}) == "allow"
        assert engine.sandbox_policy.enabled is True

    @pytest.mark.asyncio
    async def test_bootstrap_binds_workspace_permission_scope(
        self,
        temp_data_dir,
        temp_workspace,
    ):
        (temp_data_dir / "config" / "permissions.yaml").write_text(
            "allow:\n"
            "  - tool: filesystem_write\n"
            "    paths: ${workspace}\n"
            "ask:\n"
            "  - tool: filesystem_write\n",
            encoding="utf-8",
        )
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert engine.permission_system.check(
            "filesystem_write", {"path": "notes.md"}
        ) == "allow"
        assert engine.permission_system.check(
            "filesystem_write", {"path": str(temp_data_dir / "outside.md")}
        ) == "ask"


class TestBootstrapNoPlugins:
    """Engine works correctly in explicit no-plugin mode."""

    def test_explicit_empty_plugin_dirs_disables_builtin_scan(self, tmp_path):
        """Explicit no-plugin mode stays pure even when built-ins exist."""
        builtin_dir = tmp_path / "builtin_plugins"
        builtin_dir.mkdir()

        assert _resolve_plugin_dirs([], builtin_plugins_dir=builtin_dir) == []

    def test_default_plugin_dirs_scan_builtins(self, tmp_path):
        """Default runtime mode still discovers the built-in plugin root."""
        builtin_dir = tmp_path / "builtin_plugins"
        builtin_dir.mkdir()

        assert _resolve_plugin_dirs(None, builtin_plugins_dir=builtin_dir) == [builtin_dir]

    @pytest.mark.asyncio
    async def test_engine_without_plugins_works(self, temp_data_dir, temp_workspace):
        """Core engine with no plugins runs ReAct correctly."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],  # Explicitly no plugin dirs
            llm_override=MockLLM(responses=[{"content": "I work without plugins!"}]),
        )
        engine.sandbox_policy.workspace_root = temp_workspace

        events = [e async for e in engine.run_turn("test")]
        types = [e["type"] for e in events]
        assert "turn_started" in types
        assert "assistant_message" in types


class TestMemoryLoading:
    @pytest.mark.asyncio
    async def test_memory_md_loaded_from_data_memory(self, temp_data_dir):
        """MEMORY.md in data/memory/ is loaded into SystemConfig.memory."""
        (temp_data_dir / "memory").mkdir()
        (temp_data_dir / "memory" / "MEMORY.md").write_text("# Custom Memory\n\nImportant facts.\n")

        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-test",
            thread_id="t",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )
        assert "Important facts" in getattr(engine.config, "memory", "")

    @pytest.mark.asyncio
    async def test_memory_md_missing_no_error(self, temp_data_dir):
        """Bootstrap works fine when MEMORY.md doesn't exist."""
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-missing",
            thread_id="t",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )
        assert getattr(engine.config, "memory", "") == ""
