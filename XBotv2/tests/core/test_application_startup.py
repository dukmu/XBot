"""Tests for application startup and instance construction."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def _write_plugins(data_dir, overlay):
    """Write a plugins.yaml tree overlay: {entry_id: {config: {...}, disabled}}.

    The unified configuration document is xcore.yaml; tests override plugin
    entries through plugins.yaml (config deep-merged by the loader).
    """
    entries = []
    for entry_id, patch in overlay.items():
        item = {"id": entry_id, "name": entry_id}
        if "config" in patch:
            item["config"] = patch["config"]
        if patch.get("disabled"):
            item["disabled"] = True
        entries.append(item)
    path = Path(data_dir) / "config" / "plugins.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


def _write_runtime_config(data_dir, config):
    path = Path(data_dir) / "config" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


from XBotv2.core.paths import RuntimePaths
import yaml

from XBotv2.application import start_application
from XBotv2.llm.mock import MockLLM


class TestApplicationStartupBasics:
    """Minimal application_startup without plugins."""

    @pytest.mark.asyncio
    async def test_application_startup_creates_engine(self, temp_data_dir):
        """Application startup returns a working application.engine."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "Hello!"}]),
        )
        assert application is not None
        assert application.engine.turn_count == 0

    @pytest.mark.asyncio
    async def test_noninteractive_application_startup_hides_blocking_tools(
        self, temp_data_dir
    ):
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="noninteractive",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
            interactive=False,
        )

        names = set(application.engine.tools.names())
        assert "send_message" in names
        assert "ask_user" not in names
        assert "request_permission" not in names

    @pytest.mark.asyncio
    async def test_application_startup_applies_system_tool_result_cache_limits(
        self, temp_data_dir, temp_workspace, monkeypatch
    ):
        _write_plugins(temp_data_dir, {"coretools": {"config": {
            "tool_results": {"max_inline_chars": 2048, "preview_chars": 512},
        }}})
        captured = {}

        def cache_hook(_state_store, **options):
            captured.update(options)

            async def apply(_ctx):
                return None

            return apply

        monkeypatch.setattr("XBotv2.coretools.result_cache.make_tool_result_cache_hook", cache_hook)

        await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="cache-config",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert captured == {"max_inline_chars": 2048, "preview_chars": 512}

    @pytest.mark.asyncio
    async def test_application_startup_rejects_unknown_provider(self, temp_data_dir):
        with pytest.raises(ValueError, match="Unknown provider config: typo"):
            await start_application(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                provider_name="typo",
                session_id="unknown-provider",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[]),
            )

    @pytest.mark.asyncio
    async def test_default_provider_argument_uses_merged_runtime_selection(
        self, temp_data_dir, temp_workspace
    ):
        paths = RuntimePaths.from_data_dir(temp_data_dir)
        (paths.config_dir).mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "global",
                    "providers": {
                        "global": {
                            "protocol": "mock",
                            "default_model": "global",
                            "models": [{"model": "global"}],
                        },
                        "workspace": {
                            "protocol": "mock",
                            "default_model": "workspace",
                            "models": [{"model": "workspace"}],
                        },
                    },
                },
            }]),
            encoding="utf-8",
        )
        workspace_overlay = temp_workspace / ".xbot" / "plugins.yaml"
        workspace_overlay.parent.mkdir(parents=True)
        workspace_overlay.write_text(
            yaml.safe_dump([{
                "id": "llm",
                "name": "llm",
                "config": {"default": "workspace"},
            }], sort_keys=False),
            encoding="utf-8",
        )

        application = await start_application(
            paths=paths,
            session_id="configured-provider",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert application.engine.settings.provider == "workspace"
        assert application.engine.settings.model == "workspace"

        # An explicit provider on a workspace without an overlay wins.
        plain_workspace = temp_workspace.parent / "plain-ws"
        plain_workspace.mkdir(exist_ok=True)
        explicit = await start_application(
            paths=paths,
            provider_name="global",
            session_id="explicit-provider",
            workspace_root=plain_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert explicit.engine.settings.provider == "global"
        assert explicit.engine.settings.model == "global"

    def test_cli_reports_unknown_provider_without_traceback(
        self,
        temp_data_dir,
        monkeypatch,
        capsys,
    ):
        from XBotv2.main import main

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "xbotv2",
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
            "Error: Unknown provider config: typo. "
            "Configured providers: minimax, deepseek, openai, anthropic, lmstudio.\n"
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
from XBotv2.core import Events, Tool

def runtime_tool() -> str:
    return "ok"

class InitFailPlugin:
    name = "init_fail"
    def apply(self, ctx, config=None):
        self.ctx = ctx
        self._tool_names = []
        ctx.dispose(self.on_unload)
        ctx.on(Events.SESSION_INIT, self.on_session_init)

    async def on_session_init(self, ctx):
        name = ctx.tools.register(
            Tool.from_function(runtime_tool),
            namespace="plugin:init-fail",
        )
        self._tool_names.append(name)
        raise RuntimeError("session init failed")

    async def on_unload(self):
        for name in reversed(self._tool_names):
            self.ctx.tools.unregister(name)
        Path({str(unload_marker)!r}).write_text("unloaded", encoding="utf-8")


plugin = InitFailPlugin()""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "init_fail", "version": "1.0.0"}),
            encoding="utf-8",
        )

        with pytest.raises(
            RuntimeError,
            match="session init failed",
        ):
            await start_application(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                session_id="init-fail",
                thread_id="t",
                plugin_dirs=[plugins_root],
                llm_override=MockLLM(responses=[]),
            )
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
from XBotv2.core import Events, Tool

LOG = Path({str(lifecycle_log)!r})

def runtime_tool() -> str:
    return "ok"

class NormalClosePlugin:
    name = "normal_close"
    def apply(self, ctx, config=None):
        self.ctx = ctx
        self._tool_names = []
        ctx.dispose(self.on_unload)
        ctx.on(Events.SESSION_INIT, self.on_session_init)
        ctx.on(Events.SESSION_CLOSE, self.on_session_close)

    async def on_session_init(self, ctx):
        name = ctx.tools.register(
            Tool.from_function(runtime_tool),
            namespace="plugin:normal-close",
        )
        self._tool_names.append(name)

    async def on_session_close(self, ctx):
        del ctx
        LOG.write_text("close\\n", encoding="utf-8")

    async def on_unload(self):
        for name in reversed(self._tool_names):
            self.ctx.tools.unregister(name)
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write("unload\\n")


plugin = NormalClosePlugin()""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "normal_close", "version": "1.0.0"}),
            encoding="utf-8",
        )

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="normal-close",
            thread_id="t",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[]),
        )
        loader = application.loader
        engine = application.engine
        tool_name = "plugin:normal-close:runtime_tool"
        assert loader is not None
        assert tool_name in engine.tools.registered_names()

        await engine.start_session()
        await engine.close_session()
        await application.stop()

        assert lifecycle_log.read_text(encoding="utf-8").splitlines() == [
            "close",
            "unload",
        ]
        assert tool_name not in engine.tools.registered_names()
        assert loader.loaded_ids == ()
        assert application.get("loader", strict=False) is None

    @pytest.mark.asyncio
    async def test_application_startup_rejects_path_like_identifiers(self, temp_data_dir, tmp_path):
        """Runtime identifiers cannot escape the configured data directory."""
        with pytest.raises(ValueError, match="session_id"):
            await start_application(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                session_id="../escape",
                thread_id="test-thread",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[]),
            )

        assert not (tmp_path / "escape").exists()

    @pytest.mark.asyncio
    async def test_application_startup_registers_core_tools(self, temp_data_dir):
        """Core base tools are always registered."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )
        tool_names = set(application.engine.tools.names())
        assert {
            "shell",
            "read",
            "edit",
            "path",
            "search",
            "ask_user",
            "request_permission",
            "list_shells",
            "wait_shell",
        } <= tool_names
        assert "ask" not in tool_names

    @pytest.mark.asyncio
    async def test_shipped_config_does_not_duplicate_tool_registry(
        self,
        temp_data_dir,
    ):
        # xcore.yaml is the unified configuration document; the bundled tree
        # registers the base tools without duplicating them.
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="default-tools",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert {
            "ask_user",
            "path",
            "list_shells",
            "request_permission",
            "wait_shell",
        } <= set(application.engine.tools.names())
        assert (
            application.engine.tools.names()
            == application.engine.tools.registered_names()
        )

    @pytest.mark.asyncio
    async def test_application_startup_tool_filter_limits_visible_tools(self, temp_data_dir):
        """System tool selectors restrict tools passed to the model."""
        _write_runtime_config(temp_data_dir, {"tools": ["read"]})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert application.engine.tools.names() == ("read",)
        assert [tool.name for tool in application.engine.tools.enabled()] == ["read"]

    @pytest.mark.asyncio
    async def test_application_startup_unknown_tool_filter_silently_ignored(self, temp_data_dir):
        """Unknown tool selectors are silently ignored (no tools enabled)."""
        _write_runtime_config(temp_data_dir, {"tools": ["no_such_tool"]})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )
        assert application.engine.tools.names() == ()

    @pytest.mark.asyncio
    async def test_application_startup_tool_filter_can_select_plugin_tools(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """System tool selectors are applied after plugin tools load."""
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "simple"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            """
from XBotv2.core import Tool

def _plugin_tool() -> str:
    '''Plugin tool.'''
    return "plugin ok"

class SimplePlugin:
    name = "simple"

    def apply(self, ctx, config=None):
        ctx.tools.register(Tool.from_function(_plugin_tool, name="plugin_tool"))

plugin = SimplePlugin()
"""
        )
        monkeypatch.syspath_prepend(str(plugins_root))

        _write_runtime_config(temp_data_dir, {"tools": ["plugin_tool"]})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[]),
        )

        assert application.engine.tools.names() == ("plugin_tool",)
        assert [tool.name for tool in application.engine.tools.enabled()] == [
            "plugin_tool"
        ]
        assert application.engine.tools.resolve("read") is None

    @pytest.mark.asyncio
    async def test_application_startup_registers_system_hooks(
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

        _write_plugins(temp_data_dir, {"coretools": {"config": {
            "hooks": [{
                "stage": "before/user-message-accept",
                "target": "test_personality_hooks:before_user_message",
            }],
        }}})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )

        events = [e async for e in application.engine.run_turn("hello")]

        assert events[-1]["type"] == "turn_finished"
        assert application.engine.messages[0].content == "hello from hook"

    @pytest.mark.asyncio
    async def test_application_startup_invalid_system_hook_raises(self, temp_data_dir):
        """Broken system hook declarations fail loudly."""
        _write_plugins(temp_data_dir, {"coretools": {"config": {
            "hooks": [{"stage": "turn/start", "target": "missing_module:nope"}],
        }}})

        with pytest.raises(ModuleNotFoundError):
            await start_application(
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
        (config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "coretools",
                "name": "coretools",
                "config": {
                    "hooks": [{
                        "stage": "before/user-message-accept",
                        "target": "hooks/rewrite.py:rewrite",
                    }],
                },
            }], sort_keys=False),
            encoding="utf-8",
        )
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )
        print("DIAG listener:", application._bus.listener_count("before/user-message-accept"))
        print("DIAG overlay:", (temp_workspace / ".xbot" / "plugins.yaml").is_file())

        _ = [event async for event in application.engine.run_turn("hello")]
        print("DIAG msg:", application.engine.messages[0].content)

        assert application.engine.messages[0].content == "hello from workspace"

    @pytest.mark.asyncio
    async def test_workspace_config_registers_direct_tools(
        self, temp_data_dir, temp_workspace
    ):
        config_dir = temp_workspace / ".xbot"
        tools_dir = config_dir / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "greeting.py").write_text(
            "from XBotv2.core import Tool\n\n"
            "async def workspace_greeting(name: str) -> str:\n"
            "    '''Greet one person from a workspace Tool.'''\n"
            "    return f'hello {name}'\n\n"
            "TOOLS = (Tool.from_function(workspace_greeting),)\n",
            encoding="utf-8",
        )
        (config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([
                {
                    "id": "coretools",
                    "name": "coretools",
                    "config": {
                        "workspace_tools": [{"target": "tools/greeting.py:TOOLS"}],
                    },
                },
                {
                    "id": "permissions",
                    "name": "permissions",
                    "config": {
                        "permissions": {
                            "allow": [{"tool": "workspace_greeting"}],
                        },
                    },
                },
            ], sort_keys=False),
            encoding="utf-8",
        )
        llm = MockLLM(responses=[
            {"tool_calls": [{
                "id": "call_greeting",
                "name": "workspace_greeting",
                "args": {"name": "Ada"},
            }]},
            {"content": "done"},
        ])

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="workspace-tool",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )
        events = [event async for event in application.engine.run_turn("greet Ada")]

        result = next(event for event in events if event["type"] == "tool_result")
        assert result["data"]["content"] == "hello Ada"
        assert "workspace:workspace_greeting" in application.engine.tools.names()

    @pytest.mark.asyncio
    async def test_application_startup_passes_external_plugin_configs(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """External application_startup plugin_configs reach plugin on_load."""
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

class ConfiguredPlugin:
    name = "configured"

    def apply(self, ctx, config=None):
        with open({str(output_path)!r}, "w", encoding="utf-8") as fh:
            json.dump(config or {{}}, fh, sort_keys=True)


plugin = ConfiguredPlugin()
"""
        )
        monkeypatch.syspath_prepend(str(plugin_dir))

        _write_plugins(temp_data_dir, {"configured": {"config": {"value": 42}}})
        await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugin_root],
            llm_override=MockLLM(responses=[]),
        )

        assert json.loads(output_path.read_text(encoding="utf-8")) == {"value": 42}

    @pytest.mark.asyncio
    async def test_application_startup_engine_runs_turn(self, temp_data_dir, temp_workspace):
        """Engine from application_startup can run a turn."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "Hello from application_startup!"}]),
        )
        # Override workspace for the sandbox
        application.sandbox.workspace_root = temp_workspace

        events = [e async for e in application.engine.run_turn("hi")]
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 1
        assert "Hello from application_startup!" in assistant_events[0]["data"]["content"]

    @pytest.mark.asyncio
    async def test_application_startup_creates_state(self, temp_data_dir):
        """Application startup creates the state store with messages file."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )
        assert application.state_store.session_id == "test-session"
        assert application.state_store.messages_path.exists()

    @pytest.mark.asyncio
    async def test_application_startup_includes_workspace_agents_md(self, temp_data_dir, temp_workspace):
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
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [e async for e in application.engine.run_turn("hello")]

        system = llm.get_call_messages(0)[0]
        root = ET.fromstring(system.content)
        workspace = root.find("workspace_instructions")
        assert workspace is not None
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
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="dynamic-instructions",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in application.engine.run_turn("first turn")]
        instructions.write_text("Workspace rule version two.", encoding="utf-8")
        _ = [event async for event in application.engine.run_turn("second turn")]
        instructions.unlink()
        _ = [event async for event in application.engine.run_turn("third turn")]

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
    async def test_application_startup_uses_configured_human_identity(
        self, temp_data_dir, temp_workspace
    ):
        (temp_data_dir / "config").mkdir(parents=True, exist_ok=True)
        (temp_data_dir / "config" / "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "human-7",
                        "user_name": "Ada",
                        "platform": "tui",
                    },
                },
            }]),
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="identity",
            thread_id="main",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        _ = [event async for event in application.engine.run_turn("hello")]

        root = ET.fromstring(llm.get_call_messages(0)[0].content)
        runtime = root.findtext("runtime_environment") or ""
        assert "Human: Ada (human-7)" in runtime
        assert f"- workspace: {temp_workspace}" in runtime
        assert "- tool_results: session/artifacts/tool_results/ (read-only)" in runtime

    @pytest.mark.asyncio
    async def test_application_startup_separates_configured_and_agent_instructions(
        self, temp_data_dir, temp_workspace
    ):
        _write_runtime_config(
            temp_data_dir, {"instructions": "Configured rule."}
        )
        agents_dir = temp_data_dir / ".agents"
        agents_dir.mkdir()
        (agents_dir / "default.md").write_text(
            "---\ndescription: Default Agent\nmode: all\n---\nAgent workflow.",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="instruction-sources",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in application.engine.run_turn("hello")]

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
            yaml.safe_dump([{
                "id": "workspace_instructions",
                "name": "workspace_instructions",
                "disabled": True,
            }], sort_keys=False),
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in application.engine.run_turn("hello")]

        prompt = "\n".join(str(msg.content) for msg in llm.get_call_messages(0))
        assert "must not appear" not in prompt

    @pytest.mark.asyncio
    async def test_workspace_agents_are_discovered_by_workspace_instructions(
        self, temp_data_dir, temp_workspace
    ):
        """Disabling workspace_instructions also disables workspace Agents."""
        (temp_workspace / ".agents").mkdir()
        (temp_workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Workspace reviewer\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )
        (temp_workspace / ".xbot").mkdir()
        (temp_workspace / ".xbot" / "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "workspace_instructions",
                "name": "workspace_instructions",
                "disabled": True,
            }], sort_keys=False),
            encoding="utf-8",
        )
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="workspace-disabled-agents",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=MockLLM(responses=[]),
        )

        assert application.agent_catalog.get("reviewer") is None
        assert {item.name for item in application.agent_catalog.definitions()} == {
            "default",
            "Explorer",
        }
        await application.stop()

    @pytest.mark.asyncio
    async def test_workspace_subagent_appears_in_model_catalog(
        self, temp_data_dir, temp_workspace
    ):
        (temp_workspace / ".agents").mkdir()
        (temp_workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Workspace reviewer\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )
        llm = MockLLM(responses=[{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="catalog",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = [event async for event in application.engine.run_turn("hello")]
        prompt = "\n".join(str(msg.content) for msg in llm.get_call_messages(0))
        assert "- reviewer: Workspace reviewer" in prompt
        await application.stop()

    @pytest.mark.asyncio
    async def test_shell_tool_runs_in_workspace_root(self, temp_data_dir, temp_workspace):
        """Shell tool defaults cwd to the attached workspace root."""
        _write_plugins(temp_data_dir, {"permissions": {"config": {
            "permissions": {"allow": [{"tool": "shell"}]},
        }}})
        llm = MockLLM(responses=[
            {
                "content": "checking cwd",
                "tool_calls": [
                    {"name": "shell", "args": {"command": "pwd"}, "id": "call_pwd"},
                ],
            },
            {"content": "done"},
        ])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        events = [e async for e in application.engine.run_turn("where are you?")]

        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert str(temp_workspace) in tool_result["data"]["content"]

    @pytest.mark.asyncio
    async def test_application_startup_default_session_id_is_generated(self, temp_data_dir):
        """Omitting session_id creates a fresh generated session instead of default."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        session_id = application.state_store.session_id
        assert session_id != "default"
        assert "-" in session_id
        assert (
            temp_data_dir
            / "sessions"
            / session_id
            / "threads"
            / "test-thread"
            / "state"
        ).exists()

    @pytest.mark.asyncio
    async def test_system_json_policy_files_are_ignored(self, temp_data_dir):
        """System policy has YAML sources of truth."""
        (temp_data_dir / "config" / "config.yaml").write_text(
            "permissions:\n  allow:\n    - tool: read\n"
            "sandbox:\n  enabled: true\n",
            encoding="utf-8",
        )
        (temp_data_dir / "config" / "permissions.json").write_text(
            '{"deny": [{"tool": "read"}]}'
        )
        (temp_data_dir / "config" / "sandbox.json").write_text(
            '{"enabled": false}'
        )

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert application.permissions.check("read", {}) == "allow"
        assert application.sandbox.enabled is True

    @pytest.mark.asyncio
    async def test_application_startup_binds_workspace_permission_scope(
        self,
        temp_data_dir,
        temp_workspace,
    ):
        _write_plugins(temp_data_dir, {"permissions": {"config": {
            "permissions": {
                "allow": [{"tool": "edit", "paths": "${workspace}"}],
                "ask": [{"tool": "edit"}],
            },
        }}})
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        ps = application.permissions
        assert ps.check(
            "edit", {"path": "notes.md", "mode": "write"}
        ) == "allow"
        assert ps.check(
            "edit", {"path": str(temp_data_dir / "outside.md"), "mode": "write"}
        ) == "ask"


class TestApplicationStartupNoPlugins:
    """Engine works correctly in explicit no-plugin mode."""

    def _make_tree(self, plugin_dirs, include_builtins):
        import tempfile
        from XBotv2.config.tree import load_agent_tree

        tmp = Path(tempfile.mkdtemp())
        paths = RuntimePaths.from_data_dir(tmp)
        return load_agent_tree(
            paths=paths,
            session_paths=paths.session("s"),
            session_id="s", thread_id="t",
            workspace_root=Path("."), provider_name="default",
            parent_permission_system=None, interactive=True,
            is_subagent=False,
            plugin_dirs=plugin_dirs if not include_builtins else None,
            extra_plugins=None,
        )

    def test_explicit_empty_plugin_dirs_disables_builtin_scan(self):
        """Explicit no-plugin mode stays pure even when built-ins exist."""
        tree = self._make_tree(plugin_dirs=[], include_builtins=False)
        ids = {entry.id for entry in tree.entries}
        assert "goal" not in ids
        assert "tools" in ids
        assert "agent-catalog" in ids
        assert "agent-runtime" in ids
        assert "agentloop" in ids

    def test_default_plugin_dirs_scan_builtins(self):
        """Default runtime mode includes the built-in plugins in the tree."""
        tree = self._make_tree(plugin_dirs=None, include_builtins=True)
        ids = {entry.id for entry in tree.entries}
        assert "goal" in ids
        assert "todolist" in ids
        assert "tools" in ids
        assert "agent-catalog" in ids
        assert "agent-runtime" in ids
        assert "agentloop" in ids

    @pytest.mark.asyncio
    async def test_engine_without_plugins_works(self, temp_data_dir, temp_workspace):
        """Core engine with no plugins runs ReAct correctly."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],  # Explicitly no plugin dirs
            llm_override=MockLLM(responses=[{"content": "I work without plugins!"}]),
        )
        application.sandbox.workspace_root = temp_workspace

        events = [e async for e in application.engine.run_turn("test")]
        types = [e["type"] for e in events]
        assert "turn_started" in types
        assert "assistant_message" in types

    @pytest.mark.asyncio
    async def test_runtime_can_start_without_message_persistence(
        self,
        temp_data_dir,
        temp_workspace,
    ):
        paths = RuntimePaths.from_data_dir(temp_data_dir)
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "persistence",
                "name": "persistence",
                "disabled": True,
            }]),
            encoding="utf-8",
        )
        application = await start_application(
            paths=paths,
            session_id="memory-only",
            thread_id="t",
            workspace_root=temp_workspace,
            llm_override=MockLLM(responses=[{"content": "in memory"}]),
        )
        assert application.get("state_store", strict=False) is None
        assert application.storage.root.is_dir()
        events = [
            event async for event in application.engine.run_turn("hello")
        ]
        assert any(
            event.get("type") == "assistant_message" for event in events
        )
        await application.stop()


class TestMemoryLoading:
    @pytest.mark.asyncio
    async def test_memory_md_loaded_from_data_memory(self, temp_data_dir):
        """MEMORY.md in data/memory/ is loaded into RuntimeConfig.memory."""
        (temp_data_dir / "memory").mkdir()
        (temp_data_dir / "memory" / "MEMORY.md").write_text("# Custom Memory\n\nImportant facts.\n")

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-test",
            thread_id="t",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )
        assert "Important facts" in getattr(application.engine.settings, "memory", "")

    @pytest.mark.asyncio
    async def test_memory_md_missing_no_error(self, temp_data_dir):
        """Application startup works fine when MEMORY.md doesn't exist."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-missing",
            thread_id="t",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )
        assert getattr(application.engine.settings, "memory", "") == ""
