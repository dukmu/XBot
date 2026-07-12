"""Tests for PluginLoader — discovery, dependency resolution, loading."""

import asyncio
import tempfile
import sys
import types
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from xbotv2.api.plugins import PluginBase, PluginConfigError, PluginManifest
from xbotv2.api import Tool, ToolRegistrationOptions
from xbotv2.plugin.loader import (
    LoadedPluginRecord,
    PluginLoader,
    _DefaultPlugin,
    _PluginSetupContext,
    _RuntimePluginContext,
    instantiate_plugin,
    resolve_dependencies,
)
from xbotv2.plugin.store import PluginStore
from xbotv2.core.context import ContextBuilder
from xbotv2.hooks.manager import HookManager
from xbotv2.api.hooks import HookContext, HookStage, SessionInfo
from xbotv2.persistence.store import CoreStateStore
from xbotv2.tools.registry import ToolRegistry
from xbotv2.api.paths import RuntimePaths


# ------------------------------------------------------------------
# Test manifests
# ------------------------------------------------------------------

def _make_manifest(name: str, version: str = "1.0.0", deps: list[str] | None = None) -> PluginManifest:
    return PluginManifest(name=name, version=version, depends_on=deps or [])


def _make_manifest_tuple(name: str, deps: list[str] | None = None) -> tuple[PluginManifest, Path]:
    return (_make_manifest(name, deps=deps), Path(f"/fake/{name}"))


def _setup_plugin(plugin):
    context = ContextBuilder()
    setup = _PluginSetupContext(
        plugin_name=plugin.manifest.name,
        hooks=HookManager(),
        tools=ToolRegistry(),
        context=context,
    )
    plugin.setup(setup)
    return context


# ------------------------------------------------------------------
# Dependency resolution
# ------------------------------------------------------------------

class TestDependencyResolution:
    """Topological sort of plugin manifests."""

    def test_no_dependencies(self):
        """Plugins without deps resolve in input order."""
        items = [
            _make_manifest_tuple("a"),
            _make_manifest_tuple("b"),
            _make_manifest_tuple("c"),
        ]
        result = resolve_dependencies(items)
        names = [m.name for m, _ in result]
        assert names == ["a", "b", "c"]

    def test_simple_dependency(self):
        """A depends on B → B comes before A."""
        items = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b"),
        ]
        result = resolve_dependencies(items)
        names = [m.name for m, _ in result]
        assert names.index("b") < names.index("a")

    def test_diamond_dependency(self):
        """Diamond: a→b, a→c, b→d, c→d."""
        items = [
            _make_manifest_tuple("a", deps=["b", "c"]),
            _make_manifest_tuple("b", deps=["d"]),
            _make_manifest_tuple("c", deps=["d"]),
            _make_manifest_tuple("d"),
        ]
        result = resolve_dependencies(items)
        names = [m.name for m, _ in result]
        assert names.index("d") < names.index("b")
        assert names.index("d") < names.index("c")
        assert names.index("b") < names.index("a")
        assert names.index("c") < names.index("a")

    def test_missing_dependency_raises(self):
        """Depends on nonexistent plugin raises."""
        items = [
            _make_manifest_tuple("a", deps=["nonexistent"]),
        ]
        with pytest.raises(ValueError, match="nonexistent"):
            resolve_dependencies(items)

    def test_circular_dependency_raises(self):
        """A→B, B→A raises."""
        items = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b", deps=["a"]),
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            resolve_dependencies(items)

    def test_chain_dependency(self):
        """Long chain: a→b→c→d."""
        items = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b", deps=["c"]),
            _make_manifest_tuple("c", deps=["d"]),
            _make_manifest_tuple("d"),
        ]
        result = resolve_dependencies(items)
        names = [m.name for m, _ in result]
        assert names == ["d", "c", "b", "a"]


class TestPluginManifest:
    """Manifest model validation."""

    def test_minimal_manifest(self):
        """Minimal manifest with only name and version."""
        m = PluginManifest(name="test", version="1.0.0")
        assert m.name == "test"
        assert m.version == "1.0.0"
        assert m.depends_on == []
        assert m.hooks == []
        assert m.tools == []

    @pytest.mark.asyncio
    async def test_plugin_base_has_safe_lifecycle_defaults(self):
        plugin = PluginBase(_make_manifest("minimal"), store=None)

        await plugin.on_load({})
        await plugin.on_unload()

        assert plugin.diagnostics() == {"status": "ready"}

    def test_manifest_from_yaml(self):
        """Manifest can be loaded from YAML dict."""
        data = {
            "name": "planning",
            "version": "2.0.0",
            "description": "DAG planning",
            "depends_on": ["compact"],
            "hooks": [
                {"stage": "on_session_init", "handler": "planning.hooks:on_init"},
            ],
            "tools": [
                {"handler": "planning.tools:plan_add_nodes", "sandbox_mode": "host"},
            ],
            "prompt_fragments": [
                {"stage": "context_suffix", "handler": "planning.context:render"},
            ],
        }
        m = PluginManifest(**data)
        assert m.name == "planning"
        assert len(m.hooks) == 1
        assert m.hooks[0].stage == "on_session_init"
        assert len(m.tools) == 1
        assert m.tools[0].sandbox_mode == "host"
        assert len(m.prompt_fragments) == 1

    def test_manifest_rejects_invalid_config_schema(self):
        with pytest.raises(ValidationError, match="config_schema is invalid"):
            PluginManifest(
                name="invalid",
                version="1",
                config_schema={"type": "not-a-json-schema-type"},
            )

    @pytest.mark.parametrize("name", ["../escape", "/absolute", ".hidden", "name/child"])
    def test_manifest_rejects_unsafe_plugin_names(self, name):
        with pytest.raises(ValidationError):
            PluginManifest(name=name, version="1")

    def test_validate_config_reports_plugin_and_structured_path(self):
        manifest = PluginManifest(
            name="configured",
            version="1",
            config_schema={
                "type": "object",
                "properties": {
                    "limits": {
                        "type": "object",
                        "properties": {"retries": {"type": "integer"}},
                    },
                },
            },
        )

        with pytest.raises(PluginConfigError) as captured:
            manifest.validate_config({"limits": {"retries": "three"}})

        assert captured.value.plugin_name == "configured"
        assert captured.value.path == ("limits", "retries")
        assert "$.limits.retries" in str(captured.value)

    @pytest.mark.parametrize(
        ("name", "valid_config", "invalid_config"),
        [
            ("skills", {}, {"unknown": True}),
            (
                "token_manager",
                {"max_context_tokens": 8192, "soft_limit_ratio": 0.75},
                {"soft_limit_ratio": 2},
            ),
            (
                "mcp",
                {"servers": {"local": {"command": ["mcp-server"]}}},
                {"servers": {"remote": {"type": "remote"}}},
            ),
        ],
    )
    def test_builtin_manifest_config_schemas(self, name, valid_config, invalid_config):
        manifest_data = yaml.safe_load(
            Path(f"XBotv2/builtin_plugins/{name}/plugin.yaml").read_text(
                encoding="utf-8"
            )
        )
        manifest = PluginManifest(**manifest_data)

        manifest.validate_config(valid_config)
        with pytest.raises(PluginConfigError):
            manifest.validate_config(invalid_config)


class TestPromptFragmentFiles:
    """Static prompt fragments resolve relative to the plugin directory."""

    def test_default_plugin_loads_prompt_file_relative_to_plugin_dir(self, tmp_path):
        plugin_dir = tmp_path / "plugins" / "simple"
        prompts_dir = plugin_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "instructions.md").write_text("## Static Instructions\nUse care.\n")

        manifest = PluginManifest(
            name="simple",
            version="1.0.0",
            prompt_fragments=[
                {"stage": "system_instructions", "file": "prompts/instructions.md"}
            ],
            plugin_dir=plugin_dir,
        )
        plugin = _DefaultPlugin(manifest, store=None)

        context = _setup_plugin(plugin)

        assert context.get_fragment("system_instructions", "simple") == "## Static Instructions\nUse care.\n"

    def test_plugin_base_loads_prompt_file_relative_to_plugin_dir(self, tmp_path):
        plugin_dir = tmp_path / "plugins" / "classy"
        prompts_dir = plugin_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "rules.md").write_text("## Plugin Rules\nStay isolated.\n")

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        manifest = PluginManifest(
            name="classy",
            version="1.0.0",
            prompt_fragments=[
                {"stage": "system_rules", "file": "prompts/rules.md"}
            ],
            plugin_dir=plugin_dir,
        )

        class ClassyPlugin(PluginBase):
            async def on_load(self, config: dict) -> None:
                self._config = config

        plugin = ClassyPlugin(manifest, PluginStore(state_store, "classy"))

        context = _setup_plugin(plugin)

        assert context.get_fragment("system_rules", "classy") == "## Plugin Rules\nStay isolated.\n"

    def test_default_plugin_missing_prompt_file_raises(self, tmp_path):
        plugin_dir = tmp_path / "plugins" / "broken"
        plugin_dir.mkdir(parents=True)
        manifest = PluginManifest(
            name="broken",
            version="1.0.0",
            prompt_fragments=[
                {"stage": "system_instructions", "file": "prompts/missing.md"}
            ],
            plugin_dir=plugin_dir,
        )
        plugin = _DefaultPlugin(manifest, store=None)

        with pytest.raises(FileNotFoundError, match="prompt fragment file not found"):
            _setup_plugin(plugin)

    def test_default_plugin_invalid_handler_raises(self):
        manifest = PluginManifest(
            name="broken",
            version="1.0.0",
            hooks=[
                {"stage": "on_session_init", "handler": "missing_handler_path"}
            ],
        )
        plugin = _DefaultPlugin(manifest, store=None)

        with pytest.raises(ValueError, match="Invalid handler path"):
            _setup_plugin(plugin)


class TestPluginSetupContext:
    """Setup capability behavior."""

    def test_register_tool_accepts_explicit_options(self):
        setup = _PluginSetupContext(
            plugin_name="sample",
            hooks=HookManager(),
            tools=ToolRegistry(),
            context=ContextBuilder(),
        )

        def sample_tool(path: str) -> str:
            """Sample plugin tool."""
            return path

        registered_name = setup.register_tool(
            Tool.from_function(sample_tool),
            options=ToolRegistrationOptions(
                sandbox_mode="sandboxed",
                namespace="plugin:sample",
            ),
        )

        assert registered_name == "plugin:sample:sample_tool"
        assert setup.tool_names == ["plugin:sample:sample_tool"]
        entry = setup.tools.get("plugin:sample:sample_tool")
        assert entry is not None
        assert entry.sandbox_mode == "sandboxed"
        assert entry.namespace == "plugin:sample"

    def test_register_tool_collision_does_not_replace_existing_owner(self):
        tools = ToolRegistry()

        def shared_tool() -> str:
            """Core-owned tool."""
            return "core"

        original = Tool.from_function(shared_tool)
        tools.register(original)
        setup = _PluginSetupContext(
            plugin_name="sample",
            hooks=HookManager(),
            tools=tools,
            context=ContextBuilder(),
        )

        with pytest.raises(ValueError, match="already registered"):
            setup.register_tool(Tool.from_function(shared_tool))

        assert tools.get("shared_tool").tool is original
        assert setup.tool_names == []

    def test_runtime_unregister_tool_is_limited_to_owned_registrations(self):
        tools = ToolRegistry()

        def core_tool() -> str:
            return "core"

        def runtime_tool() -> str:
            return "runtime"

        tools.register(Tool.from_function(core_tool))
        tool_names: list[str] = []
        runtime = _RuntimePluginContext("sample", tools, tool_names)
        registered_name = runtime.register_tool(
            Tool.from_function(runtime_tool),
            ToolRegistrationOptions(namespace="plugin:sample"),
        )

        assert runtime.unregister_tool("core_tool") is False
        assert tools.registered("core_tool")
        assert runtime.unregister_tool(registered_name) is True
        assert not tools.registered(registered_name)
        assert tool_names == []
        assert runtime.unregister_tool(registered_name) is False

    @pytest.mark.asyncio
    async def test_runtime_register_tool_is_recorded_for_unload(
        self, tmp_path, monkeypatch
    ):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "runtime_tools"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            """
from xbotv2.api import HookStage, PluginBase, Tool, ToolRegistrationOptions

def runtime_tool() -> str:
    \"\"\"Runtime tool.\"\"\"
    return "ok"

class RuntimeToolsPlugin(PluginBase):
    async def on_load(self, config):
        pass

    def setup(self, ctx):
        ctx.register_hook(HookStage.ON_SESSION_INIT, self.on_session_init)

    async def on_session_init(self, ctx):
        ctx.plugin_runtime.register_tool(
            Tool.from_function(runtime_tool),
            options=ToolRegistrationOptions(namespace="plugin:runtime-tools"),
        )
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "runtime_tools", "version": "1.0.0"})
        )
        monkeypatch.syspath_prepend(str(plugins_root))

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace",
            provider="default",
        )
        hook_manager = HookManager()
        tool_registry = ToolRegistry()
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=hook_manager,
            tool_registry=tool_registry,
            context_builder=ContextBuilder(),
        )
        await loader.load()
        ctx = HookContext(
            stage=HookStage.ON_SESSION_INIT,
            tools=tool_registry,
            session=SessionInfo(
                session_id="s",
                thread_id="t",
                workspace_root="/workspace",
                provider="default",
            ),
        )

        await hook_manager.run(HookStage.ON_SESSION_INIT, ctx, short_circuit=False)

        tool_name = "plugin:runtime-tools:runtime_tool"
        assert tool_registry.registered(tool_name)
        assert loader._records["runtime_tools"].tool_names == [tool_name]
        assert ctx.plugin_runtime is None

        assert await loader.unload("runtime_tools") is True
        assert not tool_registry.registered(tool_name)


class TestPluginLoader:
    """PluginLoader discovery and registration."""

    def test_instantiate_plugin_continues_past_module_without_class(
        self, tmp_path, monkeypatch
    ):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "searchable"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text("")
        (plugin_dir / "plugin.py").write_text(
            """
from xbotv2.api.plugins import PluginBase

class SearchablePlugin(PluginBase):
    async def on_load(self, config):
        pass
"""
        )
        monkeypatch.syspath_prepend(str(plugins_root))
        monkeypatch.setitem(
            sys.modules,
            "builtin_plugins.searchable.plugin",
            types.ModuleType("builtin_plugins.searchable.plugin"),
        )
        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace",
            provider="default",
        )
        manifest = PluginManifest(name="searchable", version="1.0.0")

        plugin = instantiate_plugin(manifest, PluginStore(state_store, "searchable"))

        assert plugin.__class__.__name__ == "SearchablePlugin"

    @pytest.mark.asyncio
    async def test_invalid_config_fails_before_plugin_module_import(self, tmp_path):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "configured"
        plugin_dir.mkdir(parents=True)
        import_marker = tmp_path / "imported"
        (plugin_dir / "__init__.py").write_text(
            f"from pathlib import Path\nPath({str(import_marker)!r}).touch()\n"
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({
                "name": "configured",
                "version": "1",
                "config_schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                },
            })
        )
        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace",
            provider="default",
        )
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=ToolRegistry(),
            context_builder=ContextBuilder(),
            plugin_configs={"configured": {"value": "wrong"}},
        )

        with pytest.raises(PluginConfigError, match="configured"):
            await loader.load()

        assert not import_marker.exists()
        assert loader.loaded_plugins == []

    @pytest.mark.asyncio
    async def test_loader_discovers_manifest_plugin_and_registers_fragment(self, tmp_path):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "simple"
        prompts_dir = plugin_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({
                "name": "simple",
                "version": "1.0.0",
                "prompt_fragments": [
                    {"stage": "system_instructions", "file": "prompts/instructions.md"},
                ],
            })
        )
        (prompts_dir / "instructions.md").write_text("Loader instructions\n")

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        context_builder = ContextBuilder()
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=ToolRegistry(),
            context_builder=context_builder,
        )

        manifests = loader.discover()
        plugins = await loader.load()

        assert [manifest.name for manifest, _ in manifests] == ["simple"]
        assert len(plugins) == 1
        assert "Loader instructions" in context_builder._fragments["system_instructions"]["simple"]

    @pytest.mark.asyncio
    async def test_loader_rolls_back_partial_registration_on_failure(self, tmp_path, monkeypatch):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "broken"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text("")
        (plugin_dir / "hooks.py").write_text(
            """
async def on_turn_start(ctx):
    ctx.emit({"hook": "called"})
"""
        )
        (plugin_dir / "tools.py").write_text(
            """
from langchain_core.tools import tool

@tool
def plugin_tool() -> str:
    \"\"\"Plugin tool.\"\"\"
    return "ok"
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({
                "name": "broken",
                "version": "1.0.0",
                "hooks": [
                    {"stage": "on_turn_start", "handler": "broken.hooks:on_turn_start"},
                ],
                "tools": [
                    {"handler": "broken.tools:plugin_tool"},
                ],
                "prompt_fragments": [
                    {"stage": "system_instructions", "file": "prompts/missing.md"},
                ],
            })
        )
        monkeypatch.syspath_prepend(str(plugins_root))

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        hook_manager = HookManager()
        tool_registry = ToolRegistry()
        context_builder = ContextBuilder()
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=hook_manager,
            tool_registry=tool_registry,
            context_builder=context_builder,
        )

        with pytest.raises(FileNotFoundError, match="prompt fragment file not found"):
            await loader.load()

        assert len(hook_manager._hooks.get(HookStage.ON_TURN_START, [])) == 0
        assert "plugin_tool" not in tool_registry.registered_names()
        assert "broken" not in context_builder._fragments.get("system_instructions", {})
        assert loader.loaded_plugins == []
        assert loader._records == {}

    @pytest.mark.asyncio
    async def test_loader_releases_import_path_after_failed_load(self, tmp_path):
        """A failed plugin load does not leave loader-added sys.path entries."""
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "broken"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            """
from xbotv2.api.plugins import PluginBase

class BrokenPlugin(PluginBase):
    async def on_load(self, config):
        raise RuntimeError("load failed")
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "broken", "version": "1.0.0"})
        )

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=ToolRegistry(),
            context_builder=ContextBuilder(),
        )

        assert str(plugins_root) not in sys.path
        with pytest.raises(RuntimeError, match="load failed"):
            await loader.load()

        assert str(plugins_root) not in sys.path
        assert loader.loaded_plugins == []
        assert loader._import_paths == []

    @pytest.mark.asyncio
    async def test_setup_cancellation_rolls_back_partial_registrations(
        self,
        tmp_path,
    ):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "cancelled"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            """
import asyncio
from xbotv2.api import HookStage, PluginBase, Tool

def dynamic_tool() -> str:
    return "ok"

async def on_turn_start(ctx):
    return None

class CancelledPlugin(PluginBase):
    def setup(self, ctx):
        ctx.register_hook(HookStage.ON_TURN_START, on_turn_start)
        ctx.register_tool(Tool.from_function(dynamic_tool))
        ctx.add_prompt_fragment("system_instructions", "partial")
        raise asyncio.CancelledError()

    async def on_unload(self):
        await self.store.set("cleaned", True)
""",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "cancelled", "version": "1.0.0"}),
            encoding="utf-8",
        )
        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace",
            provider="default",
        )
        hooks = HookManager()
        tools = ToolRegistry()
        context = ContextBuilder()
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=hooks,
            tool_registry=tools,
            context_builder=context,
        )

        with pytest.raises(asyncio.CancelledError):
            await loader.load()

        assert hooks._hooks.get(HookStage.ON_TURN_START, []) == []
        assert "plugin:cancelled:dynamic_tool" not in tools.registered_names()
        assert context.get_fragment("system_instructions", "cancelled") is None
        assert state_store.get_plugin_state("cancelled") == {"cleaned": True}
        assert loader.loaded_plugins == []
        assert loader._records == {}
        assert loader._import_paths == []

    @pytest.mark.asyncio
    async def test_on_load_failure_cleans_partially_initialized_plugin(
        self,
        tmp_path,
    ):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "partial"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            """
from xbotv2.api.plugins import PluginBase

class PartialPlugin(PluginBase):
    async def on_load(self, config):
        await self.store.set("allocated", True)
        raise RuntimeError("partial load failed")

    async def on_unload(self):
        await self.store.set("cleaned", True)
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "partial", "version": "1.0.0"})
        )
        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace",
            provider="default",
        )
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=ToolRegistry(),
            context_builder=ContextBuilder(),
        )

        with pytest.raises(RuntimeError, match="partial load failed"):
            await loader.load()

        assert state_store.get_plugin_state("partial") == {
            "allocated": True,
            "cleaned": True,
        }
        assert loader.loaded_plugins == []
        assert loader._records == {}
        assert loader._import_paths == []

    @pytest.mark.asyncio
    async def test_loader_rolls_back_already_loaded_plugins_when_later_plugin_fails(
        self, tmp_path
    ):
        """load() is atomic: later failures unload earlier plugins from that call."""
        plugins_root = tmp_path / "plugins"
        first_dir = plugins_root / "first"
        first_dir.mkdir(parents=True)
        (first_dir / "__init__.py").write_text(
            """
from xbotv2.api.plugins import PluginBase

class FirstPlugin(PluginBase):
    async def on_load(self, config):
        pass

    async def on_unload(self):
        await self.store.set("unloaded", True)

    def register_hooks(self, manager):
        async def on_turn_start(ctx):
            pass
        manager.register("on_turn_start", on_turn_start)
"""
        )
        (first_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "first", "version": "1.0.0"})
        )
        broken_dir = plugins_root / "broken"
        broken_dir.mkdir(parents=True)
        (broken_dir / "__init__.py").write_text(
            """
from xbotv2.api.plugins import PluginBase

class BrokenPlugin(PluginBase):
    async def on_load(self, config):
        raise RuntimeError("broken load")
"""
        )
        (broken_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "broken", "version": "1.0.0", "depends_on": ["first"]})
        )

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        hook_manager = HookManager()
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=hook_manager,
            tool_registry=ToolRegistry(),
            context_builder=ContextBuilder(),
        )

        with pytest.raises(RuntimeError, match="broken load"):
            await loader.load()

        assert loader.loaded_plugins == []
        assert loader._records == {}
        assert loader._import_paths == []
        assert len(hook_manager._hooks.get(HookStage.ON_TURN_START, [])) == 0
        assert state_store.get_plugin_state("first")["unloaded"] is True

    @pytest.mark.asyncio
    async def test_loader_unloads_manifest_plugin_resources(self, tmp_path, monkeypatch):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "simple"
        prompts_dir = plugin_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text("")
        (plugin_dir / "hooks.py").write_text(
            """
async def on_turn_start(ctx):
    ctx.emit({"hook": "called"})
"""
        )
        (plugin_dir / "tools.py").write_text(
            """
from langchain_core.tools import tool

@tool
def plugin_tool() -> str:
    \"\"\"Plugin tool.\"\"\"
    return "ok"
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({
                "name": "simple",
                "version": "1.0.0",
                "hooks": [
                    {"stage": "on_turn_start", "handler": "simple.hooks:on_turn_start"},
                ],
                "tools": [
                    {"handler": "simple.tools:plugin_tool"},
                ],
                "prompt_fragments": [
                    {"stage": "system_instructions", "file": "prompts/instructions.md"},
                ],
            })
        )
        (prompts_dir / "instructions.md").write_text("Loader instructions\n")
        monkeypatch.syspath_prepend(str(plugins_root))

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        hook_manager = HookManager()
        tool_registry = ToolRegistry()
        context_builder = ContextBuilder()
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=hook_manager,
            tool_registry=tool_registry,
            context_builder=context_builder,
        )

        await loader.load()
        assert len(hook_manager._hooks.get(HookStage.ON_TURN_START, [])) == 1
        assert tool_registry.registered("plugin_tool")
        assert "simple" in context_builder._fragments["system_instructions"]

        assert await loader.unload("simple") is True
        assert await loader.unload("simple") is False
        assert len(hook_manager._hooks.get(HookStage.ON_TURN_START, [])) == 0
        assert not tool_registry.registered("plugin_tool")
        assert "simple" not in context_builder._fragments["system_instructions"]
        assert loader.loaded_plugins == []

    @pytest.mark.asyncio
    async def test_loader_records_hidden_tools_for_unload(self, tmp_path, monkeypatch):
        """Plugin tools hidden by registry restrictions are still unload-tracked."""
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "simple"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text("")
        (plugin_dir / "tools.py").write_text(
            """
from langchain_core.tools import tool

@tool
def plugin_tool() -> str:
    \"\"\"Plugin tool.\"\"\"
    return "ok"
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

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        tool_registry = ToolRegistry()
        core_tool = type("CoreTool", (), {"name": "core_tool"})()
        tool_registry.register(core_tool)
        tool_registry.restrict(["core_tool"])
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=tool_registry,
            context_builder=ContextBuilder(),
        )

        await loader.load()

        assert "plugin_tool" in tool_registry.registered_names()
        assert tool_registry.get("plugin_tool") is None
        assert loader._records["simple"].tool_names == ["plugin_tool"]

        assert await loader.unload("simple") is True
        assert "plugin_tool" not in tool_registry.registered_names()

    @pytest.mark.asyncio
    async def test_loader_calls_plugin_on_unload(self, tmp_path, monkeypatch):
        plugins_root = tmp_path / "plugins"
        plugin_dir = plugins_root / "classy"
        plugin_dir.mkdir(parents=True)
        unload_marker = tmp_path / "unloaded.txt"
        (plugin_dir / "__init__.py").write_text(
            f"""
from xbotv2.api.plugins import PluginBase

class ClassyPlugin(PluginBase):
    async def on_load(self, config):
        self._config = config

    async def on_unload(self):
        with open({str(unload_marker)!r}, "w", encoding="utf-8") as fh:
            fh.write("unloaded")
"""
        )
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "classy", "version": "1.0.0"})
        )
        monkeypatch.syspath_prepend(str(plugins_root))

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=ToolRegistry(),
            context_builder=ContextBuilder(),
        )

        await loader.load()
        assert await loader.unload("classy") is True

        assert unload_marker.read_text(encoding="utf-8") == "unloaded"

    @pytest.mark.asyncio
    async def test_loader_unload_all_uses_reverse_load_order(self, tmp_path, monkeypatch):
        plugins_root = tmp_path / "plugins"
        order_file = tmp_path / "order.txt"
        for name in ("first", "second"):
            plugin_dir = plugins_root / name
            plugin_dir.mkdir(parents=True)
            class_name = f"{name.title()}Plugin"
            (plugin_dir / "__init__.py").write_text(
                f"""
from xbotv2.api.plugins import PluginBase

class {class_name}(PluginBase):
    async def on_load(self, config):
        pass

    async def on_unload(self):
        with open({str(order_file)!r}, "a", encoding="utf-8") as fh:
            fh.write({name!r} + "\\n")
"""
            )
            (plugin_dir / "plugin.yaml").write_text(
                yaml.safe_dump({"name": name, "version": "1.0.0"})
            )
        monkeypatch.syspath_prepend(str(plugins_root))

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace", provider="default",
        )
        loader = PluginLoader(
            plugin_dirs=[plugins_root],
            state_store=state_store,
            hook_manager=HookManager(),
            tool_registry=ToolRegistry(),
            context_builder=ContextBuilder(),
        )

        await loader.load()
        unloaded = await loader.unload_all()

        assert unloaded == ["second", "first"]
        assert order_file.read_text(encoding="utf-8").splitlines() == ["second", "first"]
        assert loader.loaded_plugins == []

    @pytest.mark.asyncio
    async def test_unload_all_cleans_every_plugin_when_one_callback_fails(self, tmp_path):
        order: list[str] = []

        class LifecyclePlugin:
            def __init__(self, name: str, *, fail: bool = False) -> None:
                self.manifest = _make_manifest(name)
                self.fail = fail

            async def on_unload(self) -> None:
                order.append(self.manifest.name)
                if self.fail:
                    raise RuntimeError(f"{self.manifest.name} cleanup failed")

        state_store = CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s"),
            thread_id="t",
            workspace_root="/workspace",
            provider="default",
        )
        hooks = HookManager()
        tools = ToolRegistry()
        context = ContextBuilder()
        loader = PluginLoader(
            plugin_dirs=[],
            state_store=state_store,
            hook_manager=hooks,
            tool_registry=tools,
            context_builder=context,
        )

        async def hook(_ctx):
            return None

        healthy = LifecyclePlugin("healthy")
        failing = LifecyclePlugin("failing", fail=True)
        for plugin in (healthy, failing):
            name = plugin.manifest.name
            hooks.register(HookStage.ON_TURN_START, hook)
            tool_name = tools.register(
                Tool.from_function(lambda: name, name=f"{name}_tool"),
            )
            context.register_fragment("system_instructions", name, name)
            loader._records[name] = LoadedPluginRecord(
                plugin=plugin,
                hook_refs=[(HookStage.ON_TURN_START, hook)],
                tool_names=[tool_name],
                fragment_stages=["system_instructions"],
            )

        with pytest.raises(BaseExceptionGroup, match="failed during unload"):
            await loader.unload_all()

        assert order == ["failing", "healthy"]
        assert loader.loaded_plugins == []
        assert loader._records == {}
        assert tools.registered_names() == []
        assert context.get_fragment("system_instructions", "healthy") is None
        assert context.get_fragment("system_instructions", "failing") is None
        assert hooks._hooks[HookStage.ON_TURN_START] == []
