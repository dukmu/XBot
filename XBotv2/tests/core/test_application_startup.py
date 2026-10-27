from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.session.api.contracts import THREAD_INFO
from XBotv2.session.api.storage import THREAD_REPOSITORY
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.tests.runtime import engine_of
from XBotv2.loader.api.contracts import LoadError
from XBotv2.session.api.contracts import SESSIONS, OpenThread
from XBotv2.llm.api.contracts import LLM
from XBotv2.core.api.errors import OperationError
"""Tests for application startup and instance construction."""

import asyncio
import base64
import json
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
import pytest_asyncio


def _write_plugin(package, source, encoding="utf-8"):
    implementation = package / "impl"
    implementation.mkdir(exist_ok=True)
    (implementation / "plugin.py").write_text(source, encoding=encoding)


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


from XBotv2.core.api.paths import RuntimePaths
import yaml
from xcore.api.errors import ServiceNotFoundError
from XBotv2.tests.runtime import thread_scope
from XBotv2.main import open_runtime
from XBotv2.loader.api.contracts import PluginOverlay
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.agentloop.api.contracts import InboxItem, HumanInput
from XBotv2.core.api.domain import InboxTarget
from XBotv2.agentloop.api.outputs import AssistantCompleted, LoopError, ToolCompleted
from XBotv2.core.api.parts import TextPart, ImagePart
from XBotv2.core.api.tools import ToolCall, ToolSucceeded
from XBotv2.core.api.artifacts import ArtifactKind, ImageRef
from XBotv2.core.api.messages import HumanInputMessage, ToolMessage
from XBotv2.core.api.provider import ProviderTool, ProviderUser, ResolvedImagePart


async def _run_turn(engine, content):
    """Run the loop through its explicit typed-input contract."""
    item = InboxItem(
        target=InboxTarget.NEXT_TURN,
        input=HumanInput(content=content),
    )
    return [event async for event in engine.run_turn(item)]


async def _hosted_application(
    *, paths, session_id, workspace_root, llm_override
):
    """Open an Agent through the process session owner used in production."""
    host = await open_runtime(
        paths=paths,
        overrides=PluginOverlay.parse([{"id": "session", "config": {
            "provider_name": "default",
            "workspace_root": str(workspace_root),
            "no_plugins": False,
        }}]),
    )
    providers = host.require(LLM)
    protocol = providers.provider_config("default").protocol
    providers.unregister(protocol)
    providers.register(protocol, lambda *_: llm_override)
    runtime = await host.sessions.open_session(
        session_id=session_id,
        thread_id="main",
        provider_name="default",
        workspace_root=str(workspace_root),
        no_plugins=False,
    )
    return host, runtime


@pytest_asyncio.fixture(autouse=True)
async def close_test_roots(monkeypatch):
    """Close the process worker even when a test only stops its thread."""
    roots = []
    original = thread_scope

    async def tracked_thread_scope(**kwargs):
        scope = await original(**kwargs)
        roots.append(scope.root)
        return scope

    monkeypatch.setitem(globals(), "thread_scope", tracked_thread_scope)
    yield
    for root in reversed(roots):
        if root.is_active:
            await root.close()


@pytest.mark.asyncio
async def test_thread_artifact_roundtrip_and_scope_close(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    context = await thread_scope(
        paths=paths, session_id="artifacts", thread_id="main", workspace_root=tmp_path,
        llm_override=MockLLM(responses=[]),
    )
    try:
        artifact = (await context.artifacts.put(ArtifactKind.TOOL_RESULT, b"tool output", suffix=".txt"))
        assert (await context.artifacts.read(artifact)) == b"tool output"
        assert Path(context.artifacts.model_path(artifact)).read_bytes() == b"tool output"
        await context.stop()
        assert not context.has("artifacts")
    finally:
        await context.root.close()


@pytest.mark.asyncio
async def test_persistence_mount_identity_does_not_control_history(tmp_path, caplog):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "plugins.yaml").write_text(yaml.safe_dump([
        {"id": "persistence", "disabled": True},
        {"id": "caption", "disabled": True},
        {"id": "conversation-storage", "name": "persistence"},
    ]), encoding="utf-8")

    async def start(mode="new"):
        return await thread_scope(mode=mode,
            paths=paths, session_id="custom-mount", workspace_root=tmp_path,
            llm_override=MockLLM(responses=[{"content": "reply"}]),
        )

    application = await start()
    try:
        events = await _run_turn(engine_of(application), "remember this")
        assert not any(event.kind == "error" for event in events)
        assert await application.root.require(ARCHIVE).turn_count("custom-mount", "main") == 1
        await application.state.namespace("extension").set("value", "durable")
    finally:
        await application.root.close()
    resumed = await start(mode="resume")
    try:
        assert engine_of(resumed).messages[0].parts[0].text == "remember this"
        assert engine_of(resumed).turn_count == 1
        assert await resumed.state.namespace("extension").get("value") == "durable"
        assert "ServiceConflictError" not in caplog.text
    finally:
        await resumed.root.close()




@pytest.mark.asyncio
async def test_direct_restore_from_another_directory_uses_saved_workspace(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    persisted = tmp_path / "persisted-workspace"
    requested = tmp_path / "requested-workspace"
    persisted.mkdir()
    requested.mkdir()

    first = await thread_scope(
        paths=paths,
        session_id="workspace-owner",
        workspace_root=persisted,
        no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "saved"}]),
    )
    await _run_turn(engine_of(first), "save workspace")
    await first.root.close()

    restored = await thread_scope(
            mode="resume", paths=paths,
            session_id="workspace-owner",
            workspace_root=None,
            no_plugins=True,
            llm_override=MockLLM(responses=[]),
    )
    try:
        assert restored.loop_state.metadata.value.workspace_root == str(persisted.resolve())
        assert Path(restored.get("workspace_root")) == persisted.resolve()
    finally:
        await restored.root.close()

    restored = await thread_scope(
        mode="resume", paths=paths,
        session_id="workspace-owner",
        workspace_root=persisted,
        no_plugins=True,
        llm_override=MockLLM(responses=[]),
    )
    await restored.root.close()


@pytest.mark.asyncio
async def test_runtime_state_is_shared_restorable_and_thread_local(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")

    async def start(thread_id, mode="new"):
        return await thread_scope(mode=mode,
            paths=paths, session_id="state-owner", thread_id=thread_id,
            workspace_root=tmp_path, llm_override=MockLLM(responses=[]),

        )

    first = await start("first")
    try:
        assert not paths.session("state-owner").thread("first").root.exists()
        plugin_state = first.state.namespace("extension")
        persisted_view = first.root.require(PERSISTENCE).state("state-owner", "first").namespace("extension")
        await first.require(THREAD_REPOSITORY).ensure()
        # Load both views before writing: separate caches would lose a key.
        assert await plugin_state.keys() == []
        assert await persisted_view.keys() == []
        await plugin_state.set("first", 1)
        await persisted_view.set("second", 2)
        assert {key: await plugin_state.get(key) for key in await plugin_state.keys()} == {"first": 1, "second": 2}
    finally:
        await first.root.close()
    assert paths.session("state-owner").database_file.exists()

    restored = await start("first", mode="resume")
    try:
        state = restored.state.namespace("extension")
        assert {key: await state.get(key) for key in await state.keys()} == {
            "first": 1, "second": 2,
        }
        sibling = await restored.root.require(SESSIONS).open_thread(OpenThread(
            "state-owner", "second", "first", str(tmp_path), None, "new", True,
            model_override=MockLLM(),
        ))
        try:
            assert await sibling.state.namespace("extension").keys() == []
            assert not paths.session("state-owner").thread("second").root.exists()
        finally:
            await restored.root.require(SESSIONS).close_thread("state-owner", "second")
        assert not paths.session("state-owner").thread("second").root.exists()
    finally:
        await restored.root.close()


@pytest.mark.asyncio
async def test_interaction_routing_belongs_to_the_plugin_lifecycle(tmp_path):
    from XBotv2.interactions.api.models import Answered

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    parent = await thread_scope(
        paths=paths, session_id="routing", thread_id="parent",
        workspace_root=tmp_path, llm_override=MockLLM(responses=[]),
    )
    received = []

    async def answer(request):
        received.append(request.question)
        return Answered(answer="continue")

    dispose_sink = parent.client_events.install(answer)
    child = None
    try:
        child = await parent.root.require(SESSIONS).open_thread(OpenThread(
            "routing", "child", "parent", str(tmp_path), None, "new", True,
            model_override=MockLLM(),
        ))
        response = await child.interactions.request_user_input("child question")
        assert response == Answered(answer="continue")
        assert received == ["child question"]
        await parent.root.require(SESSIONS).close_thread("routing", "child")
        assert not child.has("client_events")
        # Unloading the child must not remove the parent's routing capability.
        response = await parent.interactions.request_user_input("parent question")
        assert response == Answered(answer="continue")
        assert received == ["child question", "parent question"]
    finally:
        if child is not None:
            await parent.root.require(SESSIONS).close_thread("routing", "child")
        dispose_sink()
        await parent.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["import", "apply", "config", "dependency"])
async def test_optional_plugin_failure_does_not_prevent_an_agent_turn(tmp_path, failure):
    name = f"optional_failure_{failure}"
    plugin_root = tmp_path / "plugins"
    package = plugin_root / name
    package.mkdir(parents=True)
    source = {
        "import": 'raise RuntimeError("optional import failed")\n',
        "apply": '''
class Plugin:
    def apply(self, ctx, config):
        raise RuntimeError("optional apply failed")
def mount_thread(ctx, config):
    return Plugin().apply(ctx, config)
mount_thread.inject = getattr(Plugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(Plugin, "Config", None)
''',
        "config": '''
from pydantic import BaseModel
class Config(BaseModel):
    required_integer: int
class Plugin:
    Config = Config
    def apply(self, ctx, config):
        pass
def mount_thread(ctx, config):
    return Plugin().apply(ctx, config)
mount_thread.inject = getattr(Plugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(Plugin, "Config", None)
''',
        "dependency": '''
class Plugin:
    inject = ["unavailable_optional_service"]
    def apply(self, ctx, config):
        raise AssertionError("must not activate")
def mount_thread(ctx, config):
    return Plugin().apply(ctx, config)
mount_thread.inject = getattr(Plugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(Plugin, "Config", None)
''',
    }[failure]
    _write_plugin(package, source)
    llm = MockLLM(responses=[{"content": "healthy reply"}])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        workspace_root=tmp_path, plugin_dirs=[plugin_root], llm_override=llm,
        extra_plugins=[
            {"id": name, "name": f"{name}.impl.plugin", "optional": True},
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
        ],
    )
    try:
        events = await _run_turn(engine_of(application), "hello")
        assert not [event for event in events if isinstance(event, LoopError)]
        completed = next(event for event in events if isinstance(event, AssistantCompleted))
        assert [part.text for part in completed.message.parts if isinstance(part, TextPart)] == ["healthy reply"]
        assert len(llm.request_history) == 1
    finally:
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_entry", [
    {"id": "permissions", "disabled": True},
    {"id": "permissions", "config": {"default_decision": "not-a-decision"}},
])
async def test_agent_cannot_activate_without_its_permission_guard(tmp_path, policy_entry):
    llm = MockLLM(responses=[{"content": "must not run"}])
    with pytest.raises(LoadError, match="permissions"):
        await thread_scope(
            paths=RuntimePaths.from_data_dir(tmp_path / "data"),
            workspace_root=tmp_path, llm_override=llm,
            extra_plugins=[policy_entry],
        )
    assert not llm.request_history


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
    application = await thread_scope(
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
    artifact = (await application.artifacts.put(
        ArtifactKind.MEDIA,
        payload,
        media_type="image/png",
        name="diagram.png",
        suffix=".png",
    ))
    image = ImageRef(
        artifact_id=artifact.id,
        media_type=artifact.media_type,
        size=artifact.size,
    )
    model_path = application.artifacts.model_path(artifact)

    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
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
        await application.root.close()

    assert not [event for event in events if isinstance(event, LoopError)]
    assert resolved.ref == image
    assert resolved.absolute_path == model_path
    assert Path(resolved.absolute_path).read_bytes() == payload
    human = next(message for message in history if isinstance(message, HumanInputMessage))
    image_part = next(part for part in human.parts if isinstance(part, ImagePart))
    assert image_part.image == image
    assert resolved.absolute_path not in str(human.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_read_media_tool_result_reaches_the_follow_up_model_request(
    temp_data_dir,
    temp_workspace,
):
    """Exercise the production tool loop that exposed image-result validation."""
    payload = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
    )
    image_path = temp_workspace / "pixel.png"
    image_path.write_bytes(payload)
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "call-read-image",
            "name": "read",
            "args": {"path": "pixel.png", "mode": "media"},
        }]},
        {"content": "The image was received."},
    ], input_modalities=["text", "image"])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="tool-image-context-e2e",
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
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
        ],
    )

    try:
        events = await _run_turn(engine_of(application), "Inspect pixel.png as media.")
        provider_tool = next(
            message
            for message in llm.request_history[-1].messages
            if isinstance(message, ProviderTool)
        )
        resolved = next(
            part for part in provider_tool.parts
            if isinstance(part, ResolvedImagePart)
        )
        tool_message = next(
            message for message in application.loop_state.history.snapshot()
            if isinstance(message, ToolMessage)
        )
        logical = next(
            part for part in tool_message.outcome.output.parts
            if isinstance(part, ImagePart)
        )
    finally:
        await application.root.close()

    assert not [event for event in events if isinstance(event, LoopError)]
    assert Path(resolved.absolute_path).read_bytes() == payload
    assert resolved.ref == logical.image
    assert resolved.absolute_path not in str(tool_message.model_dump(mode="json"))


def _system_prompt(provider, request_index=-1):
    message = provider.request_history[request_index].messages[0]
    return "\n".join(part.text for part in message.parts)




class TestApplicationStartupBasics:
    """Minimal application_startup without plugins."""

    @pytest.mark.asyncio
    async def test_application_startup_creates_engine(self, temp_data_dir):
        """Application startup returns a working engine_of(application)."""
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "Hello!"}]),
        )
        assert application is not None
        assert engine_of(application).turn_count == 0

    @pytest.mark.asyncio
    async def test_noninteractive_application_startup_hides_blocking_tools(
        self, temp_data_dir
    ):
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="noninteractive",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
            interactive=False,
        )

        names = set(engine_of(application).tools.names())
        assert "notify_user" in names
        assert "ask_user" not in names
        assert "request_permission" not in names

    @pytest.mark.asyncio
    async def test_application_startup_rejects_unknown_provider(self, temp_data_dir):
        with pytest.raises(LoadError, match="Unknown provider config: typo"):
            await thread_scope(
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

        application = await thread_scope(
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
        explicit = await thread_scope(
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
        assert captured.err.startswith("Error:")
        assert "Cannot open thread scope" in captured.err
        assert "Unknown provider config: typo" in captured.err
        assert "Configured providers:" in captured.err
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
        called_marker = tmp_path / "called.txt"
        _write_plugin(plugin_dir,
            f"""
from pathlib import Path
from XBotv2.agentloop.api.events import THREAD_INITIALIZED
from XBotv2.core.api.tools import Tool

def runtime_tool() -> str:
    return "ok"

class InitFailPlugin:
    inject = ("tools",)
    name = "init_fail"
    def apply(self, ctx, config=None):
        self.ctx = ctx
        ctx.dispose(self.dispose)
        ctx.on(THREAD_INITIALIZED, self.on_session_init)

    async def on_session_init(self, event):
        del event
        Path({str(called_marker)!r}).write_text("called", encoding="utf-8")
        name = self.ctx.tools.register(
            Tool.from_function(runtime_tool),
            namespace="plugin:init-fail",
            # The plugin fiber owns this late registration.
            owner=self.ctx,
        )
        raise RuntimeError("session init failed")

    async def dispose(self):
        Path({str(dispose_marker)!r}).write_text("disposed", encoding="utf-8")


def mount_thread(ctx, config):
    return InitFailPlugin().apply(ctx, config)
mount_thread.inject = getattr(InitFailPlugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(InitFailPlugin, "Config", None)""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "init_fail", "version": "1.0.0"}),
            encoding="utf-8",
        )

        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="init-fail",
            thread_id="t",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[{"content": "healthy reply"}]),
            extra_plugins=[
                {"id": "init_fail", "name": "init_fail.impl.plugin", "optional": True},
                {"id": "caption", "config": {"auto": False, "allow_access": False}},
            ],
        )
        try:
            from XBotv2.agentloop.api.contracts import TOOLS
            assert called_marker.read_text(encoding="utf-8") == "called"
            assert dispose_marker.read_text(encoding="utf-8") == "disposed"
            assert application.require(TOOLS).resolve("runtime_tool") is None
            events = await _run_turn(engine_of(application), "hello")
            assert any(isinstance(event, AssistantCompleted) for event in events)
            assert not any(isinstance(event, LoopError) for event in events)
        finally:
            await application.root.close()
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
        _write_plugin(plugin_dir,
            f"""
from pathlib import Path
from XBotv2.agentloop.api.events import THREAD_INITIALIZED
from XBotv2.agentloop.api.events import Events
from XBotv2.core.api.tools import Tool

LOG = Path({str(lifecycle_log)!r})

def runtime_tool() -> str:
    return "ok"

class NormalClosePlugin:
    inject = ("tools",)
    name = "normal_close"
    def apply(self, ctx, config=None):
        self.ctx = ctx
        ctx.dispose(self.dispose)
        ctx.on(THREAD_INITIALIZED, self.on_session_init)
        ctx.on(Events.SESSION_CLOSE, self.on_session_close)

    async def on_session_init(self, event):
        del event
        name = self.ctx.tools.register(
            Tool.from_function(runtime_tool),
            namespace="plugin:normal-close",
            owner=self.ctx,
        )

    async def on_session_close(self, ctx):
        del ctx
        LOG.write_text("close\\n", encoding="utf-8")

    async def dispose(self):
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write("dispose\\n")


def mount_thread(ctx, config):
    return NormalClosePlugin().apply(ctx, config)
mount_thread.inject = getattr(NormalClosePlugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(NormalClosePlugin, "Config", None)""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "normal_close", "version": "1.0.0"}),
            encoding="utf-8",
        )

        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="normal-close",
            thread_id="t",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )
        engine = engine_of(application)
        tool_name = "plugin:normal-close:runtime_tool"
        assert application.get("loader", strict=False) is application.root.get("loader", strict=False)
        assert tool_name in engine.tools.registered_names()

        await engine.start_session()
        await engine.close_session()
        await application.root.close()

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
            await thread_scope(
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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )
        tool_names = set(engine_of(application).tools.names())
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
    async def test_default_policy_allows_workspace_edits_and_tool_owns_escape(
        self, temp_data_dir, temp_workspace
    ):
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="default-permissions",
            thread_id="main",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[{"content": "session title"}]),
        )
        try:
            for mode in ("write", "replace", "patch"):
                assert application.permissions.check(
                    "edit", {"path": "ordinary.txt", "mode": mode}
                ) == "allow"

            shell = engine_of(application).tools.resolve("shell")
            assert shell is not None and shell.sandbox_escape is not None
            escape = shell.sandbox_escape({
                "command": "pwd",
                "sandbox_permissions": "require_escalated",
                "justification": "inspect the host",
            })
            assert escape is not None
            assert escape.argument == "sandbox_permissions"
            assert "inspect the host" in escape.reason

            assert all(
                "sandbox_permissions" not in rule.param_patterns
                for policy in application.settings.permission_policies()
                for rule in policy.rules
            ), "the default permission policy must not restate tool arguments"
        finally:
            await application.root.close()

    @pytest.mark.asyncio
    async def test_shipped_config_does_not_duplicate_tool_registry(
        self,
        temp_data_dir,
    ):
        # xcore.yaml is the unified configuration document; the bundled tree
        # registers the base tools without duplicating them.
        application = await thread_scope(
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
        } <= set(engine_of(application).tools.names())
        assert (
            engine_of(application).tools.names()
            == engine_of(application).tools.registered_names()
        )

    @pytest.mark.asyncio
    async def test_application_startup_tool_filter_limits_visible_tools(self, temp_data_dir):
        """System tool selectors restrict tools passed to the model."""
        _write_runtime_config(temp_data_dir, {"enabled_tools": ["read"]})

        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        assert engine_of(application).tools.names() == ("read",)
        assert [tool.name for tool in engine_of(application).tools.enabled()] == ["read"]

    @pytest.mark.asyncio
    async def test_application_startup_unknown_tool_filter_silently_ignored(self, temp_data_dir):
        """Unknown tool selectors are silently ignored (no tools enabled)."""
        _write_runtime_config(temp_data_dir, {"enabled_tools": ["no_such_tool"]})

        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )
        assert engine_of(application).tools.names() == ()

    @pytest.mark.asyncio
    async def test_application_startup_tool_filter_can_select_plugin_tools(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """System tool selectors are applied after plugin tools load."""
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "simple"
        plugin_dir.mkdir(parents=True)
        _write_plugin(plugin_dir,
            """
from XBotv2.core.api.tools import Tool

def _plugin_tool() -> str:
    '''Plugin tool.'''
    return "plugin ok"

class SimplePlugin:
    inject = ("tools",)
    name = "simple"

    def apply(self, ctx, config=None):
        ctx.tools.register(Tool.from_function(_plugin_tool, name="plugin_tool"))

def mount_thread(ctx, config):
    return SimplePlugin().apply(ctx, config)
mount_thread.inject = getattr(SimplePlugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(SimplePlugin, "Config", None)
"""
        )
        monkeypatch.syspath_prepend(str(plugins_root))

        _write_runtime_config(temp_data_dir, {"enabled_tools": ["plugin_tool"]})

        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugins_root],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        assert engine_of(application).tools.names() == ("plugin_tool",)
        assert [tool.name for tool in engine_of(application).tools.enabled()] == [
            "plugin_tool"
        ]
        assert engine_of(application).tools.resolve("read") is None

    @pytest.mark.asyncio
    async def test_workspace_config_registers_direct_tools(
        self, temp_data_dir, temp_workspace
    ):
        config_dir = temp_workspace / ".xbot"
        tools_dir = config_dir / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "greeting.py").write_text(
            "from XBotv2.core.api.tools import Tool\n\n"
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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="workspace-tool",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=MockLLM(responses=[]),
        )

        assert engine_of(application).tools.resolve("workspace:workspace_greeting") is not None

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
        _write_plugin(plugin_dir,
            f"""
import json

class ConfiguredPlugin:
    name = "configured"

    def apply(self, ctx, config=None):
        with open({str(output_path)!r}, "w", encoding="utf-8") as fh:
            json.dump(config or {{}}, fh, sort_keys=True)


def mount_thread(ctx, config):
    return ConfiguredPlugin().apply(ctx, config)
mount_thread.inject = getattr(ConfiguredPlugin, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(ConfiguredPlugin, "Config", None)
"""
        )
        monkeypatch.syspath_prepend(str(plugin_dir))

        _write_plugins(temp_data_dir, {"configured": {"config": {"value": 42}}})
        await thread_scope(
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
        application = await thread_scope(
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

        events = await _run_turn(engine_of(application), "hi")
        assistant_events = [e for e in events if isinstance(e, AssistantCompleted)]
        assert len(assistant_events) == 1
        assert any(
            getattr(part, "text", "") and "Hello from application_startup!" in part.text
            for part in assistant_events[0].message.parts
        )

    @pytest.mark.asyncio
    async def test_application_startup_creates_state(self, temp_data_dir):
        """Application startup creates the state store with messages file."""
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )
        assert application.require(THREAD_INFO).session_id == "test-session"
        assert not RuntimePaths.from_data_dir(temp_data_dir).session("test-session").database_file.exists()

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(engine_of(application), "hello")

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="invalid-workspace-instructions",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        events = await _run_turn(engine_of(application), "hello")

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="dynamic-instructions",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(engine_of(application), "first turn")
        instructions.write_text("Workspace rule version two.", encoding="utf-8")
        _ = await _run_turn(engine_of(application), "second turn")
        instructions.unlink()
        _ = await _run_turn(engine_of(application), "third turn")

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="identity",
            thread_id="main",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        _ = await _run_turn(engine_of(application), "hello")

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="instruction-sources",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(engine_of(application), "hello")

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            llm_override=llm,
        )

        _ = await _run_turn(engine_of(application), "hello")

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
        application = await thread_scope(
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
        await application.root.close()

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
        host, application = await _hosted_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="catalog",
            workspace_root=temp_workspace,
            llm_override=llm,
        )
        try:
            _ = await _run_turn(engine_of(application), "hello")
            prompt = _system_prompt(llm)
            assert "- reviewer: Workspace reviewer" in prompt
        finally:
            await host.close()


    @pytest.mark.asyncio
    async def test_parent_agent_runs_a_subagent_application_through_jobs(
        self, temp_data_dir, temp_workspace
    ):
        from XBotv2.core.api.provider import ProviderTool
        from XBotv2.core.api.stream import ModelCompleted
        from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule

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
        _write_plugins(temp_data_dir, {"permissions": {"config": {"rules": [{"tool_pattern": "read", "decision": "deny"}, {"tool_pattern": ".*", "decision": "allow"}]}}})
        host, application = await _hosted_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="subagent-e2e",
            workspace_root=temp_workspace,
            llm_override=llm,
        )
        try:
            events = await _run_turn(
                engine_of(application),
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
            assert job.result.final_response == (
                "The parent policy prevented file access."
            )
            child_tool_results = [
                "".join(part.text for part in message.parts if isinstance(part, TextPart))
                for request in llm.child_requests
                for message in request.messages
                if isinstance(message, ProviderTool)
            ]
            assert "Tool denied: Permission denied for tool: read" in child_tool_results
            assert all("parent-only content" not in text for text in child_tool_results)
            child = await host.sessions.thread_summary(
                "subagent-e2e", job.spec.thread_id
            )
            assert child.status == "active"
            assert child.turn_status == "idle"
            assert child.agent == "reviewer"
            assert child.parent_thread_id == "main"
        finally:
            await host.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["failed_running", "cancelled_running"])
    async def test_subagent_failure_and_cancel_keep_child_thread_available(
        self, temp_data_dir, temp_workspace, terminal
    ):
        import asyncio

        from XBotv2.core.api.domain import ProviderError
        from XBotv2.core.api.stream import ModelCompleted, ModelFailed
        from XBotv2.persistence.impl.session_lock import acquire_session

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
        host, application = await _hosted_application(
            paths=paths,
            session_id=session_id,
            workspace_root=temp_workspace,
            llm_override=llm,
        )
        with pytest.raises(OperationError, match="already owned"):
            acquire_session(paths.session(session_id).root, label="observer")
        turn = asyncio.create_task(_run_turn(
            engine_of(application),
            "Ask the reviewer agent to inspect this change and wait for it.",
        ))
        try:
            await asyncio.wait_for(child_started.wait(), timeout=3)
            jobs = application.jobs
            assert jobs is not None
            job = next(item for item in jobs.all() if item.kind == "subagent")

            if terminal == "cancelled_running":
                await jobs.cancel(job.id)

            events = await asyncio.wait_for(turn, timeout=5)
            assert any(isinstance(event, AssistantCompleted) for event in events)
            assert job.status == terminal
            child = await host.sessions.thread_summary(
                session_id, job.spec.thread_id
            )
            assert child.status == "active"
            assert child.turn_status == "idle"
            assert child.parent_thread_id == "main"
        finally:
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            await host.close()

    @pytest.mark.asyncio
    async def test_multiple_subagents_cancel_together_keep_their_threads(
        self, temp_data_dir, temp_workspace
    ):
        import asyncio

        from XBotv2.core.api.stream import ModelCompleted
        from XBotv2.persistence.impl.session_lock import acquire_session

        (temp_workspace / ".agents").mkdir()
        (temp_workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Workspace reviewer\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )
        both_children_started = asyncio.Event()

        class ConcurrentChildrenLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.parent_calls = 0
                self.child_calls = 0

            async def _astream_once(self, request):
                self._state.request_history.append(request)
                system_text = "\n".join(
                    part.text for part in request.messages[0].parts
                )
                if "Review." in system_text:
                    self.child_calls += 1
                    if self.child_calls == 2:
                        both_children_started.set()
                    await asyncio.Event().wait()

                responses = [
                    {"content": "session title"},
                    {"tool_calls": [
                        {
                            "id": "spawn-first",
                            "name": "spawn_subagent",
                            "args": {"agent": "reviewer", "prompt": "First."},
                        },
                        {
                            "id": "spawn-second",
                            "name": "spawn_subagent",
                            "args": {"agent": "reviewer", "prompt": "Second."},
                        },
                    ]},
                    {"content": "Both reviews started."},
                ]
                response = responses[self.parent_calls]
                self.parent_calls += 1
                yield ModelCompleted(response=self.to_response(response))

        paths = RuntimePaths.from_data_dir(temp_data_dir)
        host, application = await _hosted_application(
            paths=paths,
            session_id="subagent-concurrent-cancel",
            workspace_root=temp_workspace,
            llm_override=ConcurrentChildrenLLM(),
        )
        with pytest.raises(OperationError, match="already owned"):
            acquire_session(paths.session("subagent-concurrent-cancel").root, label="observer")
        try:
            await _run_turn(engine_of(application), "Start both reviewers.")
            await asyncio.wait_for(both_children_started.wait(), timeout=3)
            jobs = application.jobs
            children = [job for job in jobs.all() if job.kind == "subagent"]
            assert len(children) == 2

            stopped = await jobs.stop_all()
            waited = await jobs.wait([job.id for job in children], timeout=1)

            assert {view.state for view in stopped} == {"cancelled_running"}
            assert waited.pending == ()
            assert {view.state for view in waited.ready} == {"cancelled_running"}
            summaries = await host.sessions.list_threads(
                "subagent-concurrent-cancel"
            )
            child_threads = [item for item in summaries if item.kind == "subagent"]
            assert len(child_threads) == 2
            assert all(item.status == "active" for item in child_threads)
            assert all(item.turn_status == "idle" for item in child_threads)
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_parent_reads_failed_subagent_and_continues_the_turn(
        self, temp_data_dir, temp_workspace
    ):
        from XBotv2.core.api.domain import ProviderError
        from XBotv2.core.api.stream import ModelCompleted, ModelFailed
        from XBotv2.core.api.tools import ToolFailed

        (temp_workspace / ".agents").mkdir()
        (temp_workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Workspace reviewer\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )

        class FailedChildLLM(MockLLM):
            def __init__(self):
                super().__init__(responses=[])
                self.parent_responses = iter([
                    {"content": "session title"},
                    {"tool_calls": [{
                        "id": "spawn-reviewer",
                        "name": "spawn_subagent",
                        "args": {"agent": "reviewer", "prompt": "Inspect."},
                    }]},
                    {"tool_calls": [{
                        "id": "wait-reviewer",
                        "name": "wait_subagent",
                        "args": {"ids": ["subagent_1"]},
                    }]},
                    {"tool_calls": [{
                        "id": "read-reviewer",
                        "name": "read_subagent",
                        "args": {"id": "subagent_1"},
                    }]},
                    {"content": "The reviewer failed; I handled the failure."},
                ])

            async def _astream_once(self, request):
                self._state.request_history.append(request)
                system_text = "\n".join(
                    part.text for part in request.messages[0].parts
                )
                if "Review." in system_text:
                    yield ModelFailed(error=ProviderError(
                        code="review_provider_failed",
                        message="review provider failed",
                        retryable=False,
                        category="provider",
                    ))
                    return
                yield ModelCompleted(
                    response=self.to_response(next(self.parent_responses))
                )

        host, application = await _hosted_application(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="subagent-parent-read-failure",
            workspace_root=temp_workspace,
            llm_override=FailedChildLLM(),
        )
        try:
            events = await _run_turn(
                engine_of(application),
                "Ask the reviewer and handle a failed review.",
            )
            tools = [
                event.execution.message
                for event in events if isinstance(event, ToolCompleted)
            ]
            assert [message.call.name for message in tools] == [
                "spawn_subagent", "wait_subagent", "read_subagent",
            ]
            assert isinstance(tools[-1].outcome, ToolFailed)
            assert tools[-1].outcome.error.code == "subagent_failed"
            assert "review provider failed" in tools[-1].outcome.error.message
            assert any(
                isinstance(event, AssistantCompleted)
                and "handled the failure" in "".join(
                    part.text for part in event.message.parts
                    if isinstance(part, TextPart)
                )
                for event in events
            )
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_goal_provider_error_disarms_without_retrying_forever(
        self, temp_data_dir, temp_workspace
    ):
        from XBotv2.commands.api.contracts import EXECUTE_COMMAND, ExecuteCommand
        from XBotv2.core.api.domain import ProviderError
        from XBotv2.core.api.stream import ModelFailed
        from XBotv2.goal.api.models import ActiveGoal

        class FailingGoalLLM(MockLLM):
            async def _astream_once(self, request):
                self._state.request_history.append(request)
                yield ModelFailed(error=ProviderError(
                    code="provider_failed",
                    message="provider failed",
                    retryable=False,
                    category="provider",
                ))

        llm = FailingGoalLLM(responses=[])
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="goal-provider-error",
            thread_id="main",
            workspace_root=temp_workspace,
            llm_override=llm,
            extra_plugins=[{"id": "goal"}],
        )
        try:
            result = await application.serial(
                EXECUTE_COMMAND.name,
                ExecuteCommand(
                    command="goal", kind="server", raw_args="finish the API"
                ),
            )
            assert result.status == "ok"
            async def wait_until_disarmed():
                while True:
                    snapshot = await application.require("goal").snapshot()
                    if snapshot.activation == "disarmed":
                        return snapshot
                    await asyncio.sleep(0.01)

            snapshot = await asyncio.wait_for(wait_until_disarmed(), timeout=5)
            assert isinstance(snapshot.state, ActiveGoal)
            assert snapshot.activation == "disarmed"
            assert len(llm.request_history) == 1
            assert engine_of(application).pending_input_count == 0
        finally:
            await application.root.close()

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            workspace_root=temp_workspace,
            plugin_dirs=[],
            llm_override=llm,
        )

        events = await _run_turn(engine_of(application), "where are you?")

        tool_result = next(e for e in events if isinstance(e, ToolCompleted))
        outcome = tool_result.execution.message.outcome
        assert isinstance(outcome, ToolSucceeded)
        assert str(temp_workspace) in "".join(
            part.text for part in outcome.output.parts
        )

    @pytest.mark.asyncio
    async def test_application_startup_default_session_id_is_generated(self, temp_data_dir):
        """Omitting session_id creates a fresh generated session instead of default."""
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            thread_id="test-thread",
            plugin_dirs=[],
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
]),
        )

        session_id = application.require(THREAD_INFO).session_id
        assert session_id != "default"
        assert "-" in session_id
        assert not (
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

        application = await thread_scope(
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
        from XBotv2.config.api.tree import load_plugin_tree

        tmp = Path(tempfile.mkdtemp())
        paths = RuntimePaths.from_data_dir(tmp)
        return load_plugin_tree(
            paths=paths,
            workspace_root=tmp,
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




    @pytest.mark.asyncio
    async def test_engine_without_plugins_works(self, temp_data_dir, temp_workspace):
        """Core engine with no plugins runs ReAct correctly."""
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            no_plugins=True,
            llm_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
{"content": "I work without plugins!"}]),
        )
        application.sandbox.workspace_root = temp_workspace

        events = await _run_turn(engine_of(application), "test")
        assert any(event.kind == "turn_started" for event in events)
        assert any(isinstance(event, AssistantCompleted) for event in events)



    def test_empty_plugin_dirs_only_disables_external_discovery(self, tmp_path):
        from XBotv2.config.api.tree import load_plugin_tree

        tree = load_plugin_tree(
            paths=RuntimePaths.from_data_dir(tmp_path / "data"),
            workspace_root=tmp_path / "workspace",
            no_plugins=False,
            plugin_dirs=[],
            extra_plugins=None,
        )
        assert "goal" in {entry.id for entry in tree.entries}

    def test_config_layers_apply_global_workspace_then_session(self, tmp_path):
        from XBotv2.config.api.tree import load_plugin_tree

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

        tree = load_plugin_tree(
            paths=paths,
            workspace_root=workspace,
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
        from XBotv2.config.api.tree import load_plugin_tree

        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        plugin_root = tmp_path / "plugins"
        plugin_root.joinpath("external", "impl").mkdir(parents=True)
        plugin_root.joinpath("external", "impl", "plugin.py").write_text("def mount_thread(ctx, config): pass\nmount_thread.inject=()\nmount_thread.provide=()\n")
        paths.config_dir.mkdir(parents=True)
        paths.config_dir.joinpath("plugins.yaml").write_text(
            yaml.safe_dump([
                {"id": "goal", "disabled": False},
                {"id": "workspace_instructions", "disabled": False},
                {"id": "external", "config": {"enabled": True}},
            ]),
            encoding="utf-8",
        )

        no_plugins_tree = load_plugin_tree(
            paths=paths,
            workspace_root=tmp_path / "workspace",
            no_plugins=True,
            plugin_dirs=[plugin_root],
            extra_plugins=[{"id": "goal", "disabled": False}],
        )
        no_plugins_ids = {entry.id for entry in no_plugins_tree.entries}
        assert "goal" not in no_plugins_ids
        assert "workspace_instructions" not in no_plugins_ids
        assert "external" not in no_plugins_ids

        subagent_tree = load_plugin_tree(
            paths=paths,
            workspace_root=tmp_path / "workspace",
            no_plugins=False,
            plugin_dirs=[plugin_root],
            extra_plugins=[{"id": "subagents", "disabled": False}],
        )
        assert "subagents" in {entry.id for entry in subagent_tree.entries}

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
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-test",
            thread_id="t",
            plugin_dirs=[],
            llm_override=llm,
        )
        await _run_turn(engine_of(application), "hello")
        assert "Important facts." in _system_prompt(llm)

    @pytest.mark.asyncio
    async def test_memory_md_missing_no_error(self, temp_data_dir):
        """Application startup works fine when MEMORY.md doesn't exist."""
        llm = MockLLM(responses=[
            {"content": "session title"},
            {"content": "ok"},
        ])
        application = await thread_scope(
            paths=RuntimePaths.from_data_dir(temp_data_dir),
            session_id="mem-missing",
            thread_id="t",
            plugin_dirs=[],
            llm_override=llm,
        )
        await _run_turn(engine_of(application), "hello")
        assert "Important facts." not in _system_prompt(llm)


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_tree", [False, True])
async def test_metadata_initialization_is_durable_and_resume_is_read_only(tmp_path, monkeypatch, reverse_tree):
    import XBotv2.session.impl.manager as manager_module
    from XBotv2.loader.api.contracts import PluginTree
    original = manager_module.load_plugin_tree
    def load_tree(*args, **kwargs):
        tree = original(*args, **kwargs)
        return PluginTree(list(reversed(tree.entries))) if reverse_tree else tree
    monkeypatch.setattr(manager_module, "load_plugin_tree", load_tree)
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    first = await thread_scope(paths=paths, session_id="metadata-once", workspace_root=tmp_path, no_plugins=True, llm_override=MockLLM([{"content": "saved"}]))
    database_file = paths.session("metadata-once").database_file
    try:
        assert not database_file.exists()
        await _run_turn(engine_of(first), "persist selection")
        expected = first.loop_state.metadata.value
        facts = await first.require(THREAD_REPOSITORY).load()
        assert facts.workspace_root == str(tmp_path.resolve())
    finally:
        await first.root.close()
    resumed = await thread_scope(paths=paths, session_id="metadata-once", workspace_root=None, mode="resume", no_plugins=True, llm_override=MockLLM())
    try:
        assert resumed.loop_state.metadata.value == expected
        assert await resumed.require(THREAD_REPOSITORY).load() == facts
    finally:
        await resumed.root.close()


@pytest.mark.asyncio
async def test_explicit_model_override_precedes_registered_provider(tmp_path):
    from XBotv2.session.api.contracts import OpenSession
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    fallback, explicit = MockLLM([{"content": "configured"}]), MockLLM([{"content": "explicit"}])
    providers = root.require(LLM)
    protocol = providers.provider_config("default").protocol
    providers.unregister(protocol)
    providers.register(protocol, lambda *_: fallback)
    try:
        first = await root.require(SESSIONS).open(OpenSession("configured", "main", str(tmp_path), None, "new", True))
        second = await root.require(SESSIONS).open(OpenSession("explicit", "main", str(tmp_path), None, "new", True, model_override=explicit))
        await _run_turn(engine_of(first), "configured input")
        await _run_turn(engine_of(second), "explicit input")
        assert fallback.call_count == explicit.call_count == 1
    finally:
        await root.close()
