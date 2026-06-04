"""Tests for PluginLoader — discovery, dependency resolution, loading."""

import tempfile
from pathlib import Path

import pytest
import yaml

from xbotv2.plugin.manifest import PluginManifest
from xbotv2.plugin.base import PluginBase
from xbotv2.plugin.loader import PluginLoader, _DefaultPlugin, resolve_dependencies
from xbotv2.plugin.store import PluginStore
from xbotv2.core.context import ContextBuilder
from xbotv2.hooks.manager import HookManager
from xbotv2.persistence.store import CoreStateStore
from xbotv2.tools.registry import ToolRegistry


# ------------------------------------------------------------------
# Test manifests
# ------------------------------------------------------------------

def _make_manifest(name: str, version: str = "1.0.0", deps: list[str] | None = None) -> PluginManifest:
    return PluginManifest(name=name, version=version, depends_on=deps or [])


def _make_manifest_tuple(name: str, deps: list[str] | None = None) -> tuple[PluginManifest, Path]:
    return (_make_manifest(name, deps=deps), Path(f"/fake/{name}"))


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
                {"stage": "dag_suffix", "handler": "planning.context:render"},
            ],
        }
        m = PluginManifest(**data)
        assert m.name == "planning"
        assert len(m.hooks) == 1
        assert m.hooks[0].stage == "on_session_init"
        assert len(m.tools) == 1
        assert m.tools[0].sandbox_mode == "host"
        assert len(m.prompt_fragments) == 1


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

        fragments = plugin.get_prompt_fragments()

        assert fragments["system_instructions"] == "## Static Instructions\nUse care.\n"

    def test_plugin_base_loads_prompt_file_relative_to_plugin_dir(self, tmp_path):
        plugin_dir = tmp_path / "plugins" / "classy"
        prompts_dir = plugin_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "rules.md").write_text("## Plugin Rules\nStay isolated.\n")

        state_store = CoreStateStore.create(
            tmp_path / "state",
            session_id="s",
            thread_id="t",
            personality_id="default",
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

        fragments = plugin.get_prompt_fragments()

        assert fragments["system_rules"] == "## Plugin Rules\nStay isolated.\n"

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
            plugin.get_prompt_fragments()

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
            plugin.register_hooks(HookManager())


class TestPluginLoader:
    """PluginLoader discovery and registration."""

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
            tmp_path / "state",
            session_id="s",
            thread_id="t",
            personality_id="default",
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
            tmp_path / "state",
            session_id="s",
            thread_id="t",
            personality_id="default",
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
        assert hook_manager.count("on_turn_start") == 1
        assert tool_registry.registered("plugin_tool")
        assert "simple" in context_builder._fragments["system_instructions"]

        assert await loader.unload("simple") is True
        assert await loader.unload("simple") is False
        assert hook_manager.count("on_turn_start") == 0
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
            tmp_path / "state",
            session_id="s",
            thread_id="t",
            personality_id="default",
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
from xbotv2.plugin.base import PluginBase

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
            tmp_path / "state",
            session_id="s",
            thread_id="t",
            personality_id="default",
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
from xbotv2.plugin.base import PluginBase

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
            tmp_path / "state",
            session_id="s",
            thread_id="t",
            personality_id="default",
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
