"""Tests for application startup and instance construction."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest


def _write_plugins(data_dir, overlay):
    """Write a plugins.yaml tree overlay: {entry_id: {config: {...}, disabled}}.

    The unified configuration document is xcore.yaml; tests override plugin
    entries through plugins.yaml (config deep-merged by the loader).
    """
    entries = []
    for entry_id, patch in overlay.items():
        item = {"id": entry_id}
        if "config" in patch:
            item["config"] = patch["config"]
        if patch.get("disabled"):
            item["disabled"] = True
        entries.append(item)
    path = Path(data_dir) / "config" / "plugins.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


def _write_runtime_config(data_dir, config):
    """Write the aggregate test fixture as a plugin-tree overlay."""
    entries = []
    core = {
        key: config[key]
        for key in ("enabled_tools", "hooks", "workspace_tools")
        if key in config
    }
    if core:
        entries.append({"id": "coretools", "config": core})
    if "instructions" in config:
        entries.append({"id": "config", "config": {"instructions": config["instructions"]}})
    if "provider" in config:
        entries.append({"id": "llm", "config": {"default_provider": config["provider"]}})
    for plugin_id in ("permissions", "sandbox"):
        if plugin_id in config:
            entries.append({"id": plugin_id, "config": {plugin_id: config[plugin_id]}})
    for plugin_id, plugin in (config.get("plugins") or {}).items():
        item = {"id": plugin_id}
        if isinstance(plugin, dict) and "config" in plugin:
            item["config"] = plugin["config"]
        if isinstance(plugin, dict) and plugin.get("enabled") is False:
            item["disabled"] = True
        entries.append(item)
    path = Path(data_dir) / "config" / "plugins.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


from XBotv2.core.paths import RuntimePaths
import yaml

from XBotv2.application.app import start_application
from XBotv2.llm.mock import MockLLM
from XBotv2.session.contracts import AgentApplicationOptions
from XBotv2.agentloop import InboxItem, InboxTarget, HumanInput
from XBotv2.agentloop.protocol import AssistantCompleted, LoopError, ToolCompleted
from XBotv2.core.parts import TextPart
from XBotv2.core.tools import ToolSucceeded
from XBotv2.core.artifacts import ArtifactKind, ImageRef
from XBotv2.core.messages import HumanInputMessage
from XBotv2.core.parts import ImagePart
from XBotv2.core.provider import ProviderUser, ResolvedImagePart


async def _run_turn(engine, content):
    """Run the loop through its explicit typed-input contract."""
    item = InboxItem(
        target=InboxTarget.NEXT_TURN,
        input=HumanInput(content=content),
    )
    return [event async for event in engine.run_turn(item)]


@pytest.mark.asyncio
async def test_application_resolves_logical_image_for_provider_without_persisting_path(
    temp_data_dir,
    temp_workspace,
):
    """An image ref stays logical in history and is resolved only per request."""
    llm = MockLLM(
        responses=[{"content": "I can inspect the image."}],
        input_modalities=["text", "image"],
    )
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="image-context-e2e",
        thread_id="main",
        workspace_root=temp_workspace,
        provider_name="mock",
        llm_override=llm,
        extra_plugins=[
            {
                "id": "llm",
                "config": {
                    "default_provider": "mock",
                    "providers": {
                        "mock": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{
                                "model": "mock",
                                "max_output_tokens": 1024,
                                "input_modalities": ["text", "image"],
                            }],
                        },
                    },
                },
            },
            {
                "id": "caption",
                "config": {"auto": False, "allow_access": False},
            },
        ],
    )
    payload = b"image payload for request projection"
    artifact = application.artifacts.put(
        ArtifactKind.MEDIA,
        payload,
        media_type="image/png",
        name="diagram.png",
        suffix=".png",
    )
    image = ImageRef(
        artifact_id=artifact.id,
        media_type=artifact.media_type,
        size=artifact.size,
    )

    try:
        events = [event async for event in application.engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Describe this diagram.", images=(image,)),
        ))]
        request = llm.request_history[0]
        provider_user = next(
            message for message in request.messages
            if isinstance(message, ProviderUser)
            and any(isinstance(part, ResolvedImagePart) for part in message.parts)
        )
        resolved = next(
            part for part in provider_user.parts
            if isinstance(part, ResolvedImagePart)
        )
        history = application.loop_state.history.snapshot()
    finally:
        await application.stop()

    assert not [event for event in events if isinstance(event, LoopError)]
    assert resolved.ref == image
    assert resolved.absolute_path == application.artifacts.model_path(artifact)
    assert Path(resolved.absolute_path).read_bytes() == payload
    human = next(message for message in history if isinstance(message, HumanInputMessage))
    image_part = next(part for part in human.parts if isinstance(part, ImagePart))
    assert image_part.image == image
    assert resolved.absolute_path not in str(human.model_dump(mode="json"))


def _system_prompt(provider, request_index=-1):
    message = provider.request_history[request_index].messages[0]
    return "\n".join(part.text for part in message.parts)


@pytest.mark.asyncio
async def test_agent_application_factory_model_override_precedence(
    tmp_path,
    monkeypatch,
):
    import XBotv2.application.app as application_app

    captured = []

    async def capture_start_application(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(application_app, "start_application", capture_start_application)
    monkeypatch.setattr(application_app, "mounted_application", lambda context: context)

    options = AgentApplicationOptions(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        provider_name="default",
        session_id="factory-session",
        thread_id="agent",
        workspace_root=tmp_path,
        no_plugins=True,
    )
    factory_default = MockLLM(responses=[])
    explicit = MockLLM(responses=[])

    await application_app.create_agent_application(
        options,
        model_override=factory_default,
    )
    await application_app.create_agent_application(
        replace(options, model_override=explicit),
        model_override=factory_default,
    )

    assert captured[0]["llm_override"] is factory_default
    assert captured[1]["llm_override"] is explicit


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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "Hello!"}]),
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
            interactive=False,
        )

        names = set(application.engine.tools.names())
        assert "send_message" in names
        assert "ask_user" not in names
        assert "request_permission" not in names

    @pytest.mark.asyncio
    async def test_application_startup_rejects_unknown_provider(self, temp_data_dir):
        with pytest.raises(ValueError, match="Unknown provider config: typo"):
            await start_application(
                paths=RuntimePaths.from_data_dir(temp_data_dir),
                provider_name="typo",
                session_id="unknown-provider",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
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
                    "default_provider": "global",
                    "providers": {
                        "global": {
                            "protocol": "mock",
                            "default_model": "global",
                            "models": [{"model": "global", "max_output_tokens": 1024}],
                        },
                        "workspace": {
                            "protocol": "mock",
                            "default_model": "workspace",
                            "models": [{"model": "workspace", "max_output_tokens": 1024}],
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
                "config": {"default_provider": "workspace"},
            }], sort_keys=False),
            encoding="utf-8",
        )

        application = await start_application(
            paths=paths,
            session_id="configured-provider",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        selection = application.agent_runtime.current_selection()
        assert selection.provider == "workspace"
        assert selection.model == "workspace"

        # An explicit provider on a workspace without an overlay wins.
        plain_workspace = temp_workspace.parent / "plain-ws"
        plain_workspace.mkdir(exist_ok=True)
        explicit = await start_application(
            paths=paths,
            provider_name="global",
            session_id="explicit-provider",
            workspace_root=plain_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        selection = explicit.agent_runtime.current_selection()
        assert selection.provider == "global"
        assert selection.model == "global"

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
    async def test_session_init_failure_disposes_runtime_plugin_resources(
        self,
        temp_data_dir,
        tmp_path,
    ):
        import sys

        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "init_fail"
        plugin_dir.mkdir(parents=True)
        dispose_marker = tmp_path / "disposed.txt"
        (plugin_dir / "__init__.py").write_text(
            f"""
from pathlib import Path
from XBotv2.application import APPLICATION_INITIALIZED
from XBotv2.core import Tool

def runtime_tool() -> str:
    return "ok"

class InitFailPlugin:
    name = "init_fail"
    def apply(self, ctx, config=None):
        self.ctx = ctx
        self._tool_names = []
        ctx.dispose(self.dispose)
        ctx.on(APPLICATION_INITIALIZED, self.on_session_init)

    async def on_session_init(self, event):
        del event
        name = self.ctx.tools.register(
            Tool.from_function(runtime_tool),
            namespace="plugin:init-fail",
            # Session-init registration: this plugin owns release itself.
            cleanup="caller",
        )
        self._tool_names.append(name)
        raise RuntimeError("session init failed")

    async def dispose(self):
        for name in reversed(self._tool_names):
            self.ctx.tools.unregister(name)
        Path({str(dispose_marker)!r}).write_text("disposed", encoding="utf-8")


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
                llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
            )
        assert dispose_marker.read_text(encoding="utf-8") == "disposed"
        assert str(plugins_root) not in sys.path

    @pytest.mark.asyncio
    async def test_normal_session_close_disposes_runtime_plugin_resources(
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
from XBotv2.application import APPLICATION_INITIALIZED
from XBotv2.agentloop import Events
from XBotv2.core import Tool

LOG = Path({str(lifecycle_log)!r})

def runtime_tool() -> str:
    return "ok"

class NormalClosePlugin:
    name = "normal_close"
    def apply(self, ctx, config=None):
        self.ctx = ctx
        self._tool_names = []
        ctx.dispose(self.dispose)
        ctx.on(APPLICATION_INITIALIZED, self.on_session_init)
        ctx.on(Events.SESSION_CLOSE, self.on_session_close)

    async def on_session_init(self, event):
        del event
        name = self.ctx.tools.register(
            Tool.from_function(runtime_tool),
            namespace="plugin:normal-close",
            cleanup="caller",
        )
        self._tool_names.append(name)

    async def on_session_close(self, ctx):
        del ctx
        LOG.write_text("close\\n", encoding="utf-8")

    async def dispose(self):
        for name in reversed(self._tool_names):
            self.ctx.tools.unregister(name)
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write("dispose\\n")


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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )
        engine = application.engine
        tool_name = "plugin:normal-close:runtime_tool"
        assert application.get("loader", strict=False) is None
        assert tool_name in engine.tools.registered_names()

        await engine.start_session()
        await engine.close_session()
        await application.destroy()

        assert lifecycle_log.read_text(encoding="utf-8").splitlines() == [
            "close",
            "dispose",
        ]
        assert tool_name not in engine.tools.registered_names()
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
                llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
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
        _write_runtime_config(temp_data_dir, {"enabled_tools": ["read"]})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        assert application.engine.tools.names() == ("read",)
        assert [tool.name for tool in application.engine.tools.enabled()] == ["read"]

    @pytest.mark.asyncio
    async def test_application_startup_unknown_tool_filter_silently_ignored(self, temp_data_dir):
        """Unknown tool selectors are silently ignored (no tools enabled)."""
        _write_runtime_config(temp_data_dir, {"enabled_tools": ["no_such_tool"]})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
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

        _write_runtime_config(temp_data_dir, {"enabled_tools": ["plugin_tool"]})

        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        assert application.engine.tools.names() == ("plugin_tool",)
        assert [tool.name for tool in application.engine.tools.enabled()] == [
            "plugin_tool"
        ]
        assert application.engine.tools.resolve("read") is None

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
                },
            ], sort_keys=False),
            encoding="utf-8",
        )
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="workspace-tool",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert application.engine.tools.resolve("workspace:workspace_greeting") is not None

    @pytest.mark.asyncio
    async def test_application_startup_passes_external_plugin_configs(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """External application startup configs reach plugin apply."""
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "Hello from application_startup!"}]),
        )
        # Override workspace for the sandbox
        application.sandbox.workspace_root = temp_workspace

        events = await _run_turn(application.engine, "hi")
        assistant_events = [e for e in events if isinstance(e, AssistantCompleted)]
        assert len(assistant_events) == 1
        assert any(
            getattr(part, "text", "") and "Hello from application_startup!" in part.text
            for part in assistant_events[0].message.parts
        )

    @pytest.mark.asyncio
    async def test_application_startup_creates_state(self, temp_data_dir):
        """Application startup creates the state store with messages file."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )
        assert application.thread_persistence.session_id == "test-session"
        assert application.thread_persistence.paths.metadata_file.exists()

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
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(application.engine, "hello")

        prompt = _system_prompt(llm)
        assert (
            f"Workspace instruction path:\n{temp_workspace}\n"
            "Keep ${workspace} and ${UNRELATED} literal."
        ) in prompt

    @pytest.mark.asyncio
    async def test_invalid_utf8_workspace_instructions_fail_before_agent_request(
        self, temp_data_dir, temp_workspace
    ):
        instructions = temp_workspace / "AGENTS.md"
        instructions.write_bytes(b"Workspace rule: \xff")
        _write_plugins(temp_data_dir, {"caption": {"disabled": True}})
        llm = MockLLM(responses=[{"content": "must not be requested"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="invalid-workspace-instructions",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        events = await _run_turn(application.engine, "hello")

        errors = [event for event in events if isinstance(event, LoopError)]
        assert len(errors) == 1
        assert errors[0].code == "engine_error"
        assert str(instructions) in errors[0].message
        assert "UTF-8" in errors[0].message
        assert not llm.request_history

    @pytest.mark.asyncio
    async def test_workspace_agents_md_is_read_between_model_requests(
        self, temp_data_dir, temp_workspace
    ):
        instructions = temp_workspace / "AGENTS.md"
        instructions.write_text("Workspace rule version one.", encoding="utf-8")
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message

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

        _ = await _run_turn(application.engine, "first turn")
        instructions.write_text("Workspace rule version two.", encoding="utf-8")
        _ = await _run_turn(application.engine, "second turn")
        instructions.unlink()
        _ = await _run_turn(application.engine, "third turn")

        # Call 0 belongs to the automatic caption; the three turns are 1..3.
        first_system = _system_prompt(llm, 1)
        second_system = _system_prompt(llm, 2)
        third_system = _system_prompt(llm, 3)
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
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="identity",
            thread_id="main",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        _ = await _run_turn(application.engine, "hello")

        prompt = _system_prompt(llm)
        assert "Human: Ada (human-7)" in prompt
        assert f"- workspace: {temp_workspace}" in prompt
        assert f"- tool_results: {application.variables['tool_results']}" in prompt

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
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="instruction-sources",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(application.engine, "hello")

        prompt = _system_prompt(llm)
        assert "Configured rule." in prompt
        assert "Agent workflow." in prompt

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
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(application.engine, "hello")

        prompt = _system_prompt(llm)
        assert "must not appear" not in prompt

    @pytest.mark.asyncio
    async def test_workspace_agents_are_independent_of_workspace_instructions(
        self, temp_data_dir, temp_workspace
    ):
        """Agent catalog ownership is independent from AGENTS.md injection."""
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        assert application.agent_catalog.get("reviewer") is not None
        assert {item.name for item in application.agent_catalog.definitions()} == {
            "default",
            "Explorer",
            "reviewer",
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
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "ok"}])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="catalog",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(application.engine, "hello")
        prompt = _system_prompt(llm)
        assert "- reviewer: Workspace reviewer" in prompt
        await application.stop()

    @pytest.mark.asyncio
    async def test_parent_agent_runs_a_subagent_application_through_jobs(
        self, temp_data_dir, temp_workspace
    ):
        from XBotv2.core.provider import ProviderTool
        from XBotv2.core.stream import ModelCompleted
        from XBotv2.permissions import PermissionPolicy, PermissionRule

        (temp_workspace / ".agents").mkdir()
        (temp_workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Workspace reviewer\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )
        secret = temp_workspace / "secret.txt"
        secret.write_text("parent-only content", encoding="utf-8")
        class AgentRoutedMockLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.parent_responses = [
                    {"content": "session title"},
                    {"tool_calls": [{
                        "id": "spawn-reviewer",
                        "name": "spawn_subagent",
                        "args": {
                            "agent": "reviewer",
                            "prompt": "Inspect the change.",
                        },
                    }]},
                    {"tool_calls": [{
                        "id": "wait-reviewer",
                        "name": "wait_subagent",
                        "args": {"mode": "all"},
                    }]},
                    {"tool_calls": [{
                        "id": "read-reviewer",
                        "name": "read_subagent",
                        "args": {"id": "subagent_1"},
                    }]},
                    {"content": "The review is complete: The parent policy prevented file access."},
                ]
                self.child_responses = [
                    {"tool_calls": [{
                        "id": "reviewer-read",
                        "name": "read",
                        "args": {"path": "secret.txt", "mode": "utf8"},
                    }]},
                    {"content": "The parent policy prevented file access."},
                ]
                self.child_requests = []

            async def _astream_once(self, request):
                system_text = "\n".join(
                    part.text for part in request.messages[0].parts
                )
                is_child = "Review." in system_text
                responses = self.child_responses if is_child else self.parent_responses
                if is_child:
                    self.child_requests.append(request)
                yield ModelCompleted(response=self.to_response(responses.pop(0)))

        llm = AgentRoutedMockLLM()
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="subagent-e2e",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
        )
        application.permissions.replace_policies((PermissionPolicy(
            default_decision="allow",
            rules=(PermissionRule(tool_pattern="read", decision="deny"),),
        ),))
        try:
            events = await _run_turn(
                application.engine,
                "Ask the reviewer agent to inspect this change, wait for it, then report.",
            )

            tool_completions = [
                event for event in events if isinstance(event, ToolCompleted)
            ]
            assert [event.execution.message.call.name for event in tool_completions] == [
                "spawn_subagent",
                "wait_subagent",
                "read_subagent",
            ]
            assert all(
                isinstance(event.execution.message.outcome, ToolSucceeded)
                for event in tool_completions
            )
            assert any(
                isinstance(event, AssistantCompleted)
                and "The review is complete: The parent policy prevented file access." in "".join(
                    part.text for part in event.message.parts
                    if isinstance(part, TextPart)
                )
                for event in events
            )

            jobs = application.jobs.all()
            assert len(jobs) == 1
            job = jobs[0]
            assert job.kind == "subagent"
            assert job.status == "succeeded"
            assert job.result is not None
            assert job.result.child.final_response == (
                "The parent policy prevented file access."
            )
            child_tool_results = [
                "".join(part.text for part in message.parts if isinstance(part, TextPart))
                for request in llm.child_requests
                for message in request.messages
                if isinstance(message, ProviderTool)
            ]
            assert "Tool execution did not produce output" in child_tool_results
            assert all("parent-only content" not in text for text in child_tool_results)
            lifecycle = application.thread_persistence.lifecycle.load()
            assert [record.event for record in lifecycle] == ["started", "completed"]
            assert lifecycle[0].thread_id != "main"
            assert lifecycle[1].thread_id == lifecycle[0].thread_id
            assert lifecycle[0].agent == "reviewer"
            assert lifecycle[0].parent_thread_id == "main"
        finally:
            await application.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["failed_running", "cancelled_running"])
    async def test_subagent_failure_and_cancel_close_child_application(
        self, temp_data_dir, temp_workspace, terminal
    ):
        import asyncio

        from XBotv2.core.domain import ProviderError
        from XBotv2.core.stream import ModelCompleted, ModelFailed
        from XBotv2.core.filesystem.session_lock import acquire_session

        (temp_workspace / ".agents").mkdir()
        (temp_workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Workspace reviewer\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )
        child_started = asyncio.Event()

        class LifecycleMockLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.parent_responses = [
                    {"content": "session title"},
                    {"tool_calls": [{
                        "id": "spawn-reviewer",
                        "name": "spawn_subagent",
                        "args": {"agent": "reviewer", "prompt": "Inspect."},
                    }]},
                    {"tool_calls": [{
                        "id": "wait-reviewer",
                        "name": "wait_subagent",
                        "args": {"mode": "all"},
                    }]},
                    {"content": "The review job reached a terminal state."},
                ]

            async def _astream_once(self, request):
                self._state.request_history.append(request)
                system_text = "\n".join(
                    part.text for part in request.messages[0].parts
                )
                if "Review." not in system_text:
                    response = self.parent_responses[self._state.call_count]
                    self._state.call_count += 1
                    yield ModelCompleted(
                        response=self.to_response(response)
                    )
                    return

                child_started.set()
                if terminal == "failed_running":
                    yield ModelFailed(error=ProviderError(
                        code="child_provider_failed",
                        message="child provider failed",
                        retryable=False,
                        category="provider",
                    ))
                    return
                await asyncio.Event().wait()

        llm = LifecycleMockLLM()
        paths = RuntimePaths.from_data_dir(temp_data_dir)
        session_id = f"subagent-{terminal}"
        application = await start_application(
            paths=paths,
            session_id=session_id,
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
        )
        owner_observer = acquire_session(
            paths.session(session_id).root,
            label="subagent-lifecycle-test-observer",
        )
        turn = asyncio.create_task(_run_turn(
            application.engine,
            "Ask the reviewer agent to inspect this change and wait for it.",
        ))
        try:
            await asyncio.wait_for(child_started.wait(), timeout=3)
            jobs = application.jobs
            assert jobs is not None
            job = next(item for item in jobs.all() if item.kind == "subagent")
            assert owner_observer.count == 3  # parent, observer, and active child

            if terminal == "cancelled_running":
                await jobs.cancel(job.id)

            events = await asyncio.wait_for(turn, timeout=5)
            assert any(isinstance(event, AssistantCompleted) for event in events)
            assert job.status == terminal
            records = application.thread_persistence.lifecycle.load()
            child_records = [
                record for record in records
                if record.thread_id != "main"
            ]
            assert [record.event for record in child_records] == [
                "started",
                "failed" if terminal == "failed_running" else "cancelled",
            ]
            assert child_records[1].error
            assert owner_observer.count == 2  # child released its session ownership
        finally:
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            await application.stop()
            owner_observer.release()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("evaluation_failure", ["malformed", "transport"])
    async def test_goal_evaluator_retries_only_after_the_retry_round_finishes(
        self, temp_data_dir, temp_workspace, evaluation_failure
    ):
        import asyncio

        from XBotv2.application.events import RUNTIME_EVENT
        from XBotv2.commands.contracts import EXECUTE_COMMAND, ExecuteCommand
        from XBotv2.core.domain import ProviderError
        from XBotv2.core.stream import ModelCompleted, ModelFailed
        from XBotv2.goal.models import AchievedGoal, GoalChanged

        class GoalRetryMockLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.retry_round_started = asyncio.Event()
                self.retry_round_release = asyncio.Event()
                self.retry_round_finished = asyncio.Event()
                self.retry_evaluation_started = asyncio.Event()
                self.first_evaluation_returned = asyncio.Event()
                self.retry_evaluation_saw_new_round = False
                self.evaluations = 0
                self.rounds = 0
                self.order = []

            async def _astream_once(self, request):
                texts = [
                    part.text
                    for message in request.messages
                    for part in message.parts
                    if isinstance(part, TextPart)
                ]
                prompt = "\n".join(texts)
                self._state.request_history.append(request)
                if "You judge whether a completion condition has been met." in prompt:
                    self.evaluations += 1
                    if self.evaluations == 1:
                        self.order.append("evaluation_failed")
                        self.first_evaluation_returned.set()
                        if evaluation_failure == "transport":
                            yield ModelFailed(error=ProviderError(
                                code="goal_evaluator_unavailable",
                                message="Goal evaluator is unavailable.",
                                retryable=False,
                                category="provider",
                            ))
                            return
                        yield ModelCompleted(
                            response=self.to_response({"content": "not a verdict"})
                        )
                        return
                    self.retry_evaluation_started.set()
                    self.retry_evaluation_saw_new_round = (
                        self.retry_round_finished.is_set()
                    )
                    self.order.append("retry_evaluation")
                    yield ModelCompleted(response=self.to_response({
                        "content": '{"verdict":"met","reason":"verified"}',
                    }))
                    return

                if '<system_reminder source="goal" event="round"' in prompt:
                    self.rounds += 1
                    if self.rounds == 2:
                        self.order.append("retry_round_started")
                        self.retry_round_started.set()
                        await self.retry_round_release.wait()
                        self.retry_round_finished.set()
                        self.order.append("retry_round_finished")
                    yield ModelCompleted(response=self.to_response({
                        "content": f"Goal work round {self.rounds}.",
                    }))
                    return

                yield ModelCompleted(response=self.to_response({
                    "content": "Session title.",
                }))

        llm = GoalRetryMockLLM()
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="goal-retry-order",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
            extra_plugins=[{
                "id": "goal",
                "config": {"max_retries": 1, "retry_seconds": 0.01},
            }],
        )
        goal_events = []
        application.on(
            RUNTIME_EVENT,
            lambda event: goal_events.append(event.event)
            if isinstance(event.event, GoalChanged)
            else None,
        )

        async def run_pending_turn():
            async for _ in application.engine.run_pending():
                pass

        retry_turn = None
        try:
            result = await application.serial(
                EXECUTE_COMMAND.name,
                ExecuteCommand(
                    command="goal",
                    kind="server",
                    raw_args="finish the API",
                ),
            )
            assert result.status == "ok"
            await run_pending_turn()
            await asyncio.wait_for(
                llm.first_evaluation_returned.wait(), timeout=3
            )
            async with asyncio.timeout(3):
                while application.engine.pending_input_count == 0:
                    await asyncio.sleep(0.001)

            retry_turn = asyncio.create_task(run_pending_turn())
            await asyncio.wait_for(llm.retry_round_started.wait(), timeout=3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    llm.retry_evaluation_started.wait(), timeout=0.05
                )
            llm.retry_round_release.set()
            await asyncio.wait_for(retry_turn, timeout=3)
            await asyncio.wait_for(llm.retry_evaluation_started.wait(), timeout=3)
            assert llm.retry_evaluation_saw_new_round
            assert llm.order.index("retry_round_finished") < llm.order.index(
                "retry_evaluation"
            )
            assert any(
                isinstance(event.snapshot.state, AchievedGoal)
                for event in goal_events
            )
        finally:
            llm.retry_round_release.set()
            if retry_turn is not None and not retry_turn.done():
                await asyncio.gather(retry_turn, return_exceptions=True)
            await application.stop()

    @pytest.mark.asyncio
    async def test_goal_clear_cancels_scheduled_evaluator_retry(
        self, temp_data_dir, temp_workspace
    ):
        import asyncio

        from XBotv2.application.events import RUNTIME_EVENT
        from XBotv2.commands.contracts import EXECUTE_COMMAND, ExecuteCommand
        from XBotv2.core.stream import ModelCompleted
        from XBotv2.goal.models import ActiveGoal, GoalChanged

        class RetryCancellationLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.rounds = 0
                self.evaluations = 0

            async def _astream_once(self, request):
                texts = [
                    part.text
                    for message in request.messages
                    for part in message.parts
                    if isinstance(part, TextPart)
                ]
                prompt = "\n".join(texts)
                if "You judge whether a completion condition has been met." in prompt:
                    self.evaluations += 1
                    yield ModelCompleted(response=self.to_response({
                        "content": "malformed evaluator output",
                    }))
                    return
                if '<system_reminder source="goal" event="round"' in prompt:
                    self.rounds += 1
                yield ModelCompleted(response=self.to_response({
                    "content": f"Work round {self.rounds}.",
                }))

        llm = RetryCancellationLLM()
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="goal-retry-clear",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
            extra_plugins=[{
                "id": "goal",
                "config": {"max_retries": 1, "retry_seconds": 0.2},
            }],
        )
        retry_scheduled = asyncio.Event()

        def observe_goal(event):
            if (
                isinstance(event.event, GoalChanged)
                and isinstance(event.event.snapshot.state, ActiveGoal)
                and event.event.snapshot.state.progress.retries == 1
            ):
                retry_scheduled.set()

        application.on(RUNTIME_EVENT, observe_goal)

        async def run_pending_turn():
            async for _ in application.engine.run_pending():
                pass

        try:
            created = await application.serial(
                EXECUTE_COMMAND.name,
                ExecuteCommand(
                    command="goal",
                    kind="server",
                    raw_args="finish the API",
                ),
            )
            assert created.status == "ok"
            await run_pending_turn()
            await asyncio.wait_for(retry_scheduled.wait(), timeout=3)
            # Let _evaluation_failed finish scheduling its owned timer before
            # clearing the condition through the public command path.
            await asyncio.sleep(0.02)

            cleared = await application.serial(
                EXECUTE_COMMAND.name,
                ExecuteCommand(
                    command="goal",
                    kind="server",
                    raw_args="clear",
                ),
            )
            assert cleared.status == "ok"
            await asyncio.sleep(0.25)

            assert llm.rounds == 1
            assert llm.evaluations == 1
            assert application.engine.pending_input_count == 0
        finally:
            await application.stop()

    @pytest.mark.asyncio
    async def test_goal_round_cap_pauses_without_starting_another_round(
        self, temp_data_dir, temp_workspace
    ):
        import asyncio

        from XBotv2.application.events import RUNTIME_EVENT
        from XBotv2.commands.contracts import EXECUTE_COMMAND, ExecuteCommand
        from XBotv2.core.stream import ModelCompleted
        from XBotv2.goal.models import GoalChanged, PausedGoal

        class RoundCapLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.rounds = 0

            async def _astream_once(self, request):
                texts = [
                    part.text
                    for message in request.messages
                    for part in message.parts
                    if isinstance(part, TextPart)
                ]
                prompt = "\n".join(texts)
                if "You judge whether a completion condition has been met." in prompt:
                    yield ModelCompleted(response=self.to_response({
                        "content": '{"verdict":"not_yet_met","reason":"More work remains."}',
                    }))
                    return
                if '<system_reminder source="goal" event="round"' in prompt:
                    self.rounds += 1
                yield ModelCompleted(response=self.to_response({
                    "content": f"Work round {self.rounds}.",
                }))

        llm = RoundCapLLM()
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="goal-round-cap",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
            extra_plugins=[{
                "id": "goal",
                "config": {"max_rounds": 1},
            }],
        )
        paused = asyncio.Event()
        paused_states = []

        def observe_goal(event):
            if (
                isinstance(event.event, GoalChanged)
                and isinstance(event.event.snapshot.state, PausedGoal)
            ):
                paused_states.append(event.event.snapshot.state)
                paused.set()

        application.on(RUNTIME_EVENT, observe_goal)
        try:
            created = await application.serial(
                EXECUTE_COMMAND.name,
                ExecuteCommand(
                    command="goal",
                    kind="server",
                    raw_args="finish the API",
                ),
            )
            assert created.status == "ok"
            async for _ in application.engine.run_pending():
                pass
            await asyncio.wait_for(paused.wait(), timeout=3)

            assert llm.rounds == 1
            assert len(paused_states) == 1
            assert paused_states[0].progress.turns_evaluated == 1
            assert paused_states[0].reason == "Round cap reached (1/1); set the goal again to continue."
            assert application.engine.pending_input_count == 0
        finally:
            await application.stop()

    @pytest.mark.asyncio
    async def test_shell_tool_runs_in_workspace_root(self, temp_data_dir, temp_workspace):
        """Shell tool defaults cwd to the attached workspace root."""
        _write_plugins(temp_data_dir, {"permissions": {"config": {
            "rules": [{"tool_pattern": "shell", "decision": "allow"}],
            "default_decision": "ask",
        }}})
        llm = MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message

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

        events = await _run_turn(application.engine, "where are you?")

        tool_result = next(e for e in events if isinstance(e, ToolCompleted))
        outcome = tool_result.execution.message.outcome
        assert isinstance(outcome, ToolSucceeded)
        assert str(temp_workspace) in "".join(
            part.text for part in outcome.output.parts
        )

    @pytest.mark.asyncio
    async def test_application_startup_default_session_id_is_generated(self, temp_data_dir):
        """Omitting session_id creates a fresh generated session instead of default."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        session_id = application.thread_persistence.session_id
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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        assert application.permissions.check("read", {}) == "allow"
        assert application.sandbox.enabled is True

class TestApplicationStartupNoPlugins:
    """Engine works correctly in explicit no-plugin mode."""

    def _make_tree(self, *, no_plugins=False, plugin_dirs=None):
        import tempfile
        from XBotv2.application.tree import load_agent_tree

        tmp = Path(tempfile.mkdtemp())
        paths = RuntimePaths.from_data_dir(tmp)
        return load_agent_tree(
            paths=paths,
            workspace_root=tmp,
            is_subagent=False,
            no_plugins=no_plugins,
            plugin_dirs=plugin_dirs,
            extra_plugins=None,
        )

    def test_explicit_no_plugins_excludes_optional_capabilities(self):
        """No-plugin mode selects the core application profile."""
        tree = self._make_tree(no_plugins=True)
        ids = {entry.id for entry in tree.entries}
        assert "goal" not in ids
        assert "agents" in ids
        assert "agentloop" in ids

    def test_default_mode_includes_optional_capabilities(self):
        """Default runtime mode includes optional built-in capabilities."""
        tree = self._make_tree()
        ids = {entry.id for entry in tree.entries}
        assert "goal" in ids
        assert "todolist" in ids
        assert "agents" in ids
        assert "agentloop" in ids

    def test_empty_plugin_dirs_only_disables_external_discovery(self, tmp_path):
        from XBotv2.application.tree import load_agent_tree

        tree = load_agent_tree(
            paths=RuntimePaths.from_data_dir(tmp_path / "data"),
            workspace_root=tmp_path / "workspace",
            is_subagent=False,
            no_plugins=False,
            plugin_dirs=[],
            extra_plugins=None,
        )
        assert "goal" in {entry.id for entry in tree.entries}

    def test_config_layers_apply_global_workspace_then_session(self, tmp_path):
        from XBotv2.application.tree import load_agent_tree

        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        paths.config_dir.mkdir(parents=True)
        paths.config_dir.joinpath("plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "goal",
                "config": {"layer": {"global": 1, "value": "global"}},
                "disabled": True,
            }]),
            encoding="utf-8",
        )
        workspace = tmp_path / "workspace"
        workspace.joinpath(".xbot").mkdir(parents=True)
        workspace.joinpath(".xbot", "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "goal",
                "config": {"layer": {"workspace": 2, "value": "workspace"}},
                "disabled": False,
            }]),
            encoding="utf-8",
        )

        tree = load_agent_tree(
            paths=paths,
            workspace_root=workspace,
            is_subagent=False,
            no_plugins=False,
            plugin_dirs=None,
            extra_plugins=[{
                "id": "goal",
                "config": {"layer": {"session": 3, "value": "session"}},
                "disabled": True,
            }],
        )
        goal = next(entry for entry in tree.entries if entry.id == "goal")
        assert goal.name == "goal"
        assert goal.disabled is True
        assert goal.config["layer"] == {
            "global": 1,
            "workspace": 2,
            "session": 3,
            "value": "session",
        }

    def test_restricted_profiles_cannot_be_reintroduced_by_overlays(self, tmp_path):
        from XBotv2.application.tree import load_agent_tree

        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        plugin_root = tmp_path / "plugins"
        plugin_root.joinpath("external").mkdir(parents=True)
        plugin_root.joinpath("external", "__init__.py").touch()
        paths.config_dir.mkdir(parents=True)
        paths.config_dir.joinpath("plugins.yaml").write_text(
            yaml.safe_dump([
                {"id": "goal", "disabled": False},
                {"id": "workspace_instructions", "disabled": False},
                {"id": "external", "config": {"enabled": True}},
            ]),
            encoding="utf-8",
        )

        no_plugins_tree = load_agent_tree(
            paths=paths,
            workspace_root=tmp_path / "workspace",
            is_subagent=False,
            no_plugins=True,
            plugin_dirs=[plugin_root],
            extra_plugins=[{"id": "goal", "disabled": False}],
        )
        no_plugins_ids = {entry.id for entry in no_plugins_tree.entries}
        assert "goal" not in no_plugins_ids
        assert "workspace_instructions" not in no_plugins_ids
        assert "external" not in no_plugins_ids

        subagent_tree = load_agent_tree(
            paths=paths,
            workspace_root=tmp_path / "workspace",
            is_subagent=True,
            no_plugins=False,
            plugin_dirs=[plugin_root],
            extra_plugins=[{"id": "subagents", "disabled": False}],
        )
        assert "subagents" not in {entry.id for entry in subagent_tree.entries}

    @pytest.mark.asyncio
    async def test_engine_without_plugins_works(self, temp_data_dir, temp_workspace):
        """Core engine with no plugins runs ReAct correctly."""
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            no_plugins=True,
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "I work without plugins!"}]),
        )
        application.sandbox.workspace_root = temp_workspace

        events = await _run_turn(application.engine, "test")
        assert any(event.kind == "turn_started" for event in events)
        assert any(isinstance(event, AssistantCompleted) for event in events)

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
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "in memory"}]),
        )
        assert application.get("thread_persistence", strict=False) is None
        assert application.artifacts is not None
        events = await _run_turn(application.engine, "hello")
        assert any(
            isinstance(event, AssistantCompleted) for event in events
        )
        await application.stop()


class TestMemoryLoading:
    @pytest.mark.asyncio
    async def test_memory_md_loaded_from_data_memory(self, temp_data_dir):
        """Data-root memory is included in the provider request."""
        (temp_data_dir / "memory").mkdir()
        (temp_data_dir / "memory" / "MEMORY.md").write_text("# Custom Memory\n\nImportant facts.\n")

        llm = MockLLM(responses=[
            {"content": "session title"},
            {"content": "ok"},
        ])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-test",
            thread_id="t",
            plugin_dirs=[],
            llm_override=llm,
        )
        await _run_turn(application.engine, "hello")
        assert "Important facts." in _system_prompt(llm)

    @pytest.mark.asyncio
    async def test_memory_md_missing_no_error(self, temp_data_dir):
        """Application startup works fine when MEMORY.md doesn't exist."""
        llm = MockLLM(responses=[
            {"content": "session title"},
            {"content": "ok"},
        ])
        application = await start_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-missing",
            thread_id="t",
            plugin_dirs=[],
            llm_override=llm,
        )
        await _run_turn(application.engine, "hello")
        assert "Important facts." not in _system_prompt(llm)
