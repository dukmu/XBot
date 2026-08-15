"""Tests for PluginLoader — discovery, dependency resolution, XCore loading."""

import asyncio
import sys
import types
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from xbotv2.api.plugins import PluginBase, PluginConfigError, PluginManifest
from xbotv2.api import (
    Command,
    CommandResult,
    Tool,
    ToolRegistrationOptions,
    RuntimeVariables,
)
from xbotv2.core.agents import AgentRegistry
from xbotv2.plugin.loader import (
    PluginLoader,
    _DefaultPlugin,
    instantiate_plugin,
    resolve_dependencies,
)
from xbotv2.core.context import ContextBuilder
from xbotv2.api.hooks import HookContext, HookStage
from xbotv2.persistence.store import CoreStateStore
from xbotv2.tools.registry import ToolRegistry
from xbotv2.api.paths import RuntimePaths
from xbotv2.plugin.bridge import register_core_services, RuntimePluginContext
from xbotv2.api.jobs import JobRegistry


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_manifest(name: str, version: str = "1.0.0", deps: list[str] | None = None) -> PluginManifest:
    return PluginManifest(name=name, version=version, depends_on=deps or [])


def _make_manifest_tuple(name: str, deps: list[str] | None = None) -> tuple[PluginManifest, Path]:
    return (_make_manifest(name, deps=deps), Path(f"/fake/{name}"))


def make_plugin_ctx(tmp_path):
    """A real XCore context with the bridge services, for loader tests."""
    from xcore import Context

    ctx = Context(data_dir=tmp_path)
    tool_registry = ToolRegistry()
    context_builder = ContextBuilder()
    agent_registry = AgentRegistry()
    job_registry = JobRegistry()
    paths = RuntimePaths.from_data_dir(tmp_path)
    register_core_services(
        ctx,
        tool_registry=tool_registry,
        context_builder=context_builder,
        agent_registry=agent_registry,
        job_registry=job_registry,
        runtime_variables=RuntimeVariables(),
        workspace_root=tmp_path,
        data_root=tmp_path,
        session=None,
        runtime_config=None,
        agent_runtime=None,
        paths=paths,
    )
    return ctx, tool_registry, context_builder, agent_registry


def _write_plugin_dir(tmp_path, name: str, code: str, manifest: dict | None = None) -> Path:
    """Write a plugin directory with plugin.py + plugin.yaml and make it importable."""
    for stale in list(sys.modules):
        if stale == name or stale.startswith(f"{name}."):
            sys.modules.pop(stale, None)
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(code, encoding="utf-8")
    data = {"name": name, "version": "1.0.0", **(manifest or {})}
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    return plugin_dir


def _plugin_apply_ctx(ctx):
    """Run a block as if inside a plugin apply (sets the caller-tracking ctx)."""
    import contextlib

    @contextlib.contextmanager
    def manager():
        from xbotv2.plugin.bridge import _active_ctx

        token = _active_ctx.set(ctx)
        try:
            yield
        finally:
            _active_ctx.reset(token)

    return manager()


# ------------------------------------------------------------------
# Dependency resolution (unchanged semantics)
# ------------------------------------------------------------------


class TestResolveDependencies:
    def test_no_dependencies(self):
        manifests = [_make_manifest_tuple("a"), _make_manifest_tuple("b")]
        assert [m.name for m, _ in resolve_dependencies(manifests)] == ["a", "b"]

    def test_simple_dependency(self):
        manifests = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b"),
        ]
        assert [m.name for m, _ in resolve_dependencies(manifests)] == ["b", "a"]

    def test_diamond_dependency(self):
        manifests = [
            _make_manifest_tuple("a", deps=["b", "c"]),
            _make_manifest_tuple("b", deps=["d"]),
            _make_manifest_tuple("c", deps=["d"]),
            _make_manifest_tuple("d"),
        ]
        result = [m.name for m, _ in resolve_dependencies(manifests)]
        assert result.index("d") < result.index("b")
        assert result.index("d") < result.index("c")
        assert result.index("b") < result.index("a")
        assert result.index("c") < result.index("a")

    def test_missing_dependency_raises(self):
        manifests = [_make_manifest_tuple("a", deps=["missing"])]
        with pytest.raises(ValueError, match="depends on 'missing'"):
            resolve_dependencies(manifests)

    def test_duplicate_manifest_name_raises_explicitly(self):
        manifests = [_make_manifest_tuple("a"), _make_manifest_tuple("a")]
        with pytest.raises(ValueError, match="Duplicate plugin manifests"):
            resolve_dependencies(manifests)

    def test_circular_dependency_raises(self):
        manifests = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b", deps=["a"]),
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            resolve_dependencies(manifests)


# ------------------------------------------------------------------
# PluginBase / manifest defaults
# ------------------------------------------------------------------


class TestPluginBase:
    def test_minimal_manifest(self):
        manifest = PluginManifest(name="x", version="1")
        assert manifest.api_version == "1"
        assert manifest.depends_on == []

    async def test_plugin_base_has_safe_lifecycle_defaults(self):
        plugin = PluginBase(_make_manifest("x"))
        await plugin.on_load({})
        await plugin.on_unload()
        assert plugin.apply(None) is None
        assert await plugin.status_slots() == {}
        assert plugin.diagnostics() == {"status": "ready"}

    def test_manifest_from_yaml(self):
        data = yaml.safe_load("name: demo\nversion: 1.2.3\n")
        manifest = PluginManifest(**data)
        assert manifest.name == "demo"
        assert manifest.version == "1.2.3"

    def test_manifest_rejects_invalid_config_schema(self):
        with pytest.raises(ValueError, match="config_schema is invalid"):
            PluginManifest(name="x", version="1", config_schema={"type": "nope"})

    def test_manifest_accepts_every_hook_stage(self):
        for stage in HookStage:
            PluginManifest(name="x", version="1", hooks=[{"stage": stage.value, "handler": "m:h"}])

    def test_manifest_rejects_unknown_hook_stage(self):
        with pytest.raises(ValidationError):
            PluginManifest(name="x", version="1", hooks=[{"stage": "unknown", "handler": "m:h"}])

    def test_manifest_rejects_unknown_tool_sandbox_mode(self):
        with pytest.raises(ValidationError):
            PluginManifest(name="x", version="1", tools=[{"handler": "m:t", "sandbox_mode": "weird"}])

    def test_manifest_rejects_unsafe_plugin_names(self):
        for name in ("../escape", "a/b", "-lead"):
            with pytest.raises(ValidationError):
                PluginManifest(name=name, version="1")

    def test_validate_config_reports_plugin_and_structured_path(self):
        manifest = PluginManifest(
            name="demo",
            version="1",
            config_schema={
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "required": ["mode"],
            },
        )
        with pytest.raises(PluginConfigError) as excinfo:
            manifest.validate_config({})
        assert excinfo.value.plugin_name == "demo"
        assert "mode" in str(excinfo.value)


# ------------------------------------------------------------------
# XCore loader behavior
# ------------------------------------------------------------------


class TestLoaderXCore:
    async def test_loader_mounts_plugin_and_registers_resources(self, tmp_path):
        code = '''
from xbotv2.api import PluginBase, Tool, ToolRegistrationOptions
from xbotv2.api.hooks import HookStage

class DemoPlugin(PluginBase):
    def apply(self, ctx):
        ctx.on(HookStage.ON_TURN_START.value, lambda hook_ctx: None)
        ctx.tools.register(
            Tool.from_function(lambda x: "ok", name="demo_tool"),
            options=ToolRegistrationOptions(sandbox_mode="host", namespace="plugin:demo"),
        )
'''
        _write_plugin_dir(tmp_path, "demo", code)
        ctx, tool_registry, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        await loader.load()
        assert [p.manifest.name for p in loader.loaded_plugins] == ["demo"]
        assert tool_registry.registered("plugin:demo:demo_tool")
        assert ctx._bus.listener_count(HookStage.ON_TURN_START.value) == 1

    async def test_loader_binds_store_and_unload_cleans_resources(self, tmp_path):
        code = '''
from xbotv2.api import PluginBase, Tool, ToolRegistrationOptions, Command, CommandResult
from xbotv2.api.hooks import HookStage

class DemoPlugin(PluginBase):
    def apply(self, ctx):
        assert self.store is not None
        ctx.on(HookStage.ON_TURN_START.value, lambda hook_ctx: None)
        ctx.tools.register(
            Tool.from_function(lambda x: "ok", name="demo_tool"),
            options=ToolRegistrationOptions(sandbox_mode="host", namespace="plugin:demo"),
        )
        ctx.commands.register(Command(name="demo", description="d", handler=lambda ctx, arg: CommandResult("ok")))
'''
        _write_plugin_dir(tmp_path, "demo", code)
        ctx, tool_registry, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        await loader.load()
        plugin = loader.loaded_plugins[0]
        assert await plugin.store.all() == {}
        await loader.unload("demo")
        assert not tool_registry.registered("plugin:demo:demo_tool")
        assert ctx._bus.listener_count(HookStage.ON_TURN_START.value) == 0
        assert loader.get_command("demo") is None

    async def test_loader_rolls_back_already_loaded_plugins_when_later_plugin_fails(self, tmp_path):
        good = '''
from xbotv2.api import PluginBase
class GoodPlugin(PluginBase):
    def apply(self, ctx):
        ctx.on("good/event", lambda x: None)
'''
        bad = '''
from xbotv2.api import PluginBase
class BadPlugin(PluginBase):
    def apply(self, ctx):
        raise RuntimeError("apply exploded")
'''
        _write_plugin_dir(tmp_path, "good", good)
        _write_plugin_dir(tmp_path, "bad", bad)
        ctx, _tools, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        with pytest.raises(RuntimeError, match="apply exploded"):
            await loader.load()
        assert loader.loaded_plugins == []
        assert ctx._bus.listener_count("good/event") == 0

    async def test_invalid_config_fails_before_plugin_module_import(self, tmp_path):
        code = '''
from xbotv2.api import PluginBase
class DemoPlugin(PluginBase):
    pass
'''
        _write_plugin_dir(
            tmp_path, "demo", code,
            manifest={"config_schema": {"type": "object", "properties": {"mode": {"type": "string"}}, "required": ["mode"]}},
        )
        ctx, _tools, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        with pytest.raises(PluginConfigError):
            await loader.load()
        assert loader.loaded_plugins == []

    async def test_on_load_failure_cleans_partially_initialized_plugin(self, tmp_path):
        code = '''
from xbotv2.api import PluginBase
class DemoPlugin(PluginBase):
    async def on_load(self, config):
        raise RuntimeError("on_load failed")
'''
        _write_plugin_dir(tmp_path, "demo", code)
        ctx, _tools, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        with pytest.raises(RuntimeError, match="on_load failed"):
            await loader.load()
        assert loader.loaded_plugins == []

    async def test_loader_unload_all_uses_reverse_load_order(self, tmp_path):
        def make(name):
            return f'''
from xbotv2.api import PluginBase
class {name.title()}Plugin(PluginBase):
    def apply(self, ctx):
        pass
'''
        _write_plugin_dir(tmp_path, "aaa", make("aaa"))
        _write_plugin_dir(tmp_path, "bbb", make("bbb"))
        ctx, _tools, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        await loader.load()
        names = [p.manifest.name for p in loader.loaded_plugins]
        assert names == ["aaa", "bbb"]
        unloaded = await loader.unload_all()
        assert unloaded == ["bbb", "aaa"]
        assert loader.loaded_plugins == []

    async def test_loader_calls_plugin_on_unload(self, tmp_path):
        code = '''
from pathlib import Path
from xbotv2.api import PluginBase
LOG = Path(r"%s")
class DemoPlugin(PluginBase):
    async def on_unload(self):
        LOG.write_text("unloaded", encoding="utf-8")
''' % str(tmp_path / "marker.txt")
        _write_plugin_dir(tmp_path, "demo", code)
        ctx, _tools, _context, _agents = make_plugin_ctx(tmp_path)
        loader = PluginLoader(ctx=ctx, plugin_dirs=[tmp_path])
        await loader.load()
        await loader.unload("demo")
        assert loader.loaded_plugins == []
        assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "unloaded"


# ------------------------------------------------------------------
# Registration ownership via services and plugin_runtime
# ------------------------------------------------------------------


class TestRegistrationOwnership:
    def test_register_tool_accepts_explicit_options(self, tmp_path):
        ctx, tool_registry, _context, _agents = make_plugin_ctx(tmp_path)
        tool = Tool.from_function(lambda: None, name="explicit_tool")
        with _plugin_apply_ctx(ctx):
            name = ctx.tools.register(
                tool,
                options=ToolRegistrationOptions(sandbox_mode="sandboxed", namespace="plugin:x"),
            )
        assert name == "plugin:x:explicit_tool"
        entry = tool_registry.get(name)
        assert entry.sandbox_mode == "sandboxed"

    async def test_runtime_register_tool_is_cleaned_on_fiber_unload(self, tmp_path):
        ctx, tool_registry, _context, _agents = make_plugin_ctx(tmp_path)

        async def plugin_body(plugin_ctx, config):
            runtime = RuntimePluginContext(
                plugin_name="demo",
                ctx=plugin_ctx,
                tools=plugin_ctx.tools,
                commands=plugin_ctx.commands,
            )
            runtime.register_tool(
                Tool.from_function(lambda: None, name="dynamic_tool"),
                options=ToolRegistrationOptions(namespace="plugin:demo"),
            )

        await ctx.start()
        handle = ctx.plugin(plugin_body)
        await handle
        assert tool_registry.registered("plugin:demo:dynamic_tool")
        await handle.dispose()
        assert not tool_registry.registered("plugin:demo:dynamic_tool")

    def test_command_registration_uses_separate_owned_registry(self, tmp_path):
        ctx, _tools, _context, _agents = make_plugin_ctx(tmp_path)
        with _plugin_apply_ctx(ctx):
            ctx.commands.register(Command(name="demo", description="d", handler=lambda ctx, arg: CommandResult("ok")))
        assert ctx.commands.get("demo") is not None

    def test_register_tool_collision_does_not_replace_existing_owner(self, tmp_path):
        ctx, tool_registry, _context, _agents = make_plugin_ctx(tmp_path)
        tool = Tool.from_function(lambda: None, name="collide")
        with _plugin_apply_ctx(ctx):
            ctx.tools.register(tool, options=ToolRegistrationOptions(namespace="plugin:a"))
        with _plugin_apply_ctx(ctx):
            with pytest.raises(ValueError, match="already registered"):
                ctx.tools.register(tool, options=ToolRegistrationOptions(namespace="plugin:b"))
