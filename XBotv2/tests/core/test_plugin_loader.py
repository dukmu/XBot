"""Tests for the plugin tree loader (cordis.yaml-style mechanism)."""

import sys
from pathlib import Path

import pytest
import yaml

from XBotv2.loader import PluginEntry, PluginTree
from XBotv2.loader.runtime import Loader, resolve_plugin_from_module


def _write_plugin(tmp_path, name: str, code: str) -> Path:
    """Write a plugin package exporting ``plugin`` and make it importable."""
    for stale in list(sys.modules):
        if stale == name or stale.startswith(f"{name}."):
            sys.modules.pop(stale, None)
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(code, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    return plugin_dir


def make_plugin_ctx(tmp_path):
    """A real XCore context with the capability services, for loader tests."""
    from xcore import Context
    from XBotv2.jobs.registry import JobRegistry
    from XBotv2.core.variables import RuntimeVariables
    from XBotv2.agents.catalog import AgentCatalog
    from XBotv2.context_builder.builder import ContextBuilder
    from XBotv2.agentloop.tool_service import ToolsService
    from XBotv2.commands.plugin import CommandsService
    from XBotv2.prompts.plugin import PromptsService
    from XBotv2.agentloop.tool_registry import ToolRegistry

    ctx = Context(data_dir=tmp_path)
    ctx.set("tools", ToolsService(ToolRegistry()))
    ctx.set("commands", CommandsService())
    ctx.set("prompts", PromptsService(ContextBuilder()))
    ctx.set("agent_catalog", AgentCatalog())
    ctx.set("jobs", JobRegistry())
    ctx.set("variables", RuntimeVariables())
    ctx.set("workspace_root", tmp_path)
    ctx.set("data_root", tmp_path)
    ctx.set("session", None)
    ctx.set("runtime", None)
    ctx.set("paths", None)
    return ctx


# ------------------------------------------------------------------
# PluginTree parsing
# ------------------------------------------------------------------


class TestPluginTree:
    def test_from_dict_list_and_nested(self):
        tree = PluginTree.from_dict([
            {"id": "a", "name": "mod.a"},
            {"id": "b", "name": "mod.b", "config": {"x": 1}, "disabled": True},
        ])
        assert [e.id for e in tree.entries] == ["a", "b"]
        assert tree.entries[1].config == {"x": 1}
        assert tree.entries[1].disabled is True

    def test_from_dict_plugins_key(self):
        tree = PluginTree.from_dict({"plugins": [{"id": "a", "name": "m"}]})
        assert tree.entries[0].id == "a"

    def test_from_yaml(self, tmp_path):
        path = tmp_path / "plugins.yaml"
        path.write_text(
            yaml.safe_dump([
                {"id": "goal", "name": "goal"},
            ]),
            encoding="utf-8",
        )
        tree = PluginTree.from_yaml(path)
        assert tree.entries[0].name == "goal"

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            PluginTree.from_dict([
                {"id": "a", "name": "m1"},
                {"id": "a", "name": "m2"},
            ])

    def test_merged_with_overrides_by_id(self):
        base = PluginTree.from_dict([{
            "id": "a",
            "name": "m1",
            "config": {"policy": {"allow": ["base"], "ask": []}},
        }])
        override = PluginTree.from_dict([
            {
                "id": "a",
                "name": "m2",
                "config": {"policy": {"ask": ["overlay"]}},
            },
            {"id": "b", "name": "m3"},
        ])
        merged = base.merged_with(override)
        ids = {e.id: e.name for e in merged.entries}
        assert ids == {"a": "m2", "b": "m3"}
        assert merged.entries[0].config == {
            "policy": {"allow": ["base"], "ask": ["overlay"]},
        }


# ------------------------------------------------------------------
# Module resolution
# ------------------------------------------------------------------


class TestResolvePlugin:
    def test_module_exports_plugin(self, tmp_path):
        _write_plugin(tmp_path, "demo", """
class DemoPlugin:
    name = "demo"

    def apply(self, ctx, config=None):
        pass

plugin = DemoPlugin()
""")
        from XBotv2.loader.runtime import Loader as _  # noqa
        import importlib

        module = importlib.import_module("demo")
        resolved = resolve_plugin_from_module(module, "demo")
        assert resolved.name == "demo"
        assert callable(resolved.apply)

    def test_module_exporting_plugin_class(self, tmp_path):
        _write_plugin(tmp_path, "demo2", """
class DemoPlugin:
    name = "demo2"

    def apply(self, ctx, config=None):
        pass

plugin = DemoPlugin()
""")
        import importlib

        module = importlib.import_module("demo2")
        assert resolve_plugin_from_module(module, "demo2").name == "demo2"

    def test_module_without_plugin_raises(self, tmp_path):
        _write_plugin(tmp_path, "empty_mod", "")
        import importlib

        module = importlib.import_module("empty_mod")
        with pytest.raises(ImportError, match="must export"):
            resolve_plugin_from_module(module, "empty_mod")


# ------------------------------------------------------------------
# Loader behavior
# ------------------------------------------------------------------


class TestLoader:
    async def test_load_mounts_entries_and_skips_disabled(self, tmp_path):
        _write_plugin(tmp_path, "alpha", """
from XBotv2.core import Tool

class AlphaPlugin:
    name = "alpha"

    def apply(self, ctx, config=None):
        ctx.tools.register(Tool.from_function(lambda: "ok", name="alpha_tool"))

plugin = AlphaPlugin()
""")
        _write_plugin(tmp_path, "beta", """
class BetaPlugin:
    name = "beta"

    def apply(self, ctx, config=None):
        pass

plugin = BetaPlugin()
""")
        ctx = make_plugin_ctx(tmp_path)
        loader = Loader(ctx, tree=PluginTree.from_dict([
            {"id": "alpha", "name": "alpha"},
            {"id": "beta", "name": "beta", "disabled": True},
        ]))
        await loader.load()
        assert loader.loaded_ids == ("alpha",)
        assert "alpha_tool" in ctx.tools.registered_names()
        assert loader.get("alpha").name == "alpha"

    async def test_unload_cleans_registrations(self, tmp_path):
        _write_plugin(tmp_path, "gamma", """
from XBotv2.core import Tool

class GammaPlugin:
    name = "gamma"

    def apply(self, ctx, config=None):
        ctx.tools.register(Tool.from_function(lambda: "ok", name="gamma_tool"))

plugin = GammaPlugin()
""")
        ctx = make_plugin_ctx(tmp_path)
        loader = Loader(ctx, tree=PluginTree.from_dict([
            {"id": "gamma", "name": "gamma"},
        ]))
        await loader.load()
        assert "gamma_tool" in ctx.tools.registered_names()
        await loader.unload("gamma")
        assert loader.loaded_ids == ()
        assert "gamma_tool" not in ctx.tools.registered_names()

    async def test_config_reaches_apply(self, tmp_path):
        _write_plugin(tmp_path, "delta", """
class DeltaPlugin:
    name = "delta"

    def apply(self, ctx, config=None):
        self.received = (config or {}).get("value")

plugin = DeltaPlugin()
""")
        ctx = make_plugin_ctx(tmp_path)
        loader = Loader(ctx, tree=PluginTree.from_dict([
            {"id": "delta", "name": "delta", "config": {"value": 42}},
        ]))
        await loader.load()
        assert loader.get("delta").received == 42

    async def test_unload_all_reverse_order(self, tmp_path):
        order = []

        def make(name):
            return f"""
class {name.title()}Plugin:
    name = "{name}"

    def apply(self, ctx, config=None):
        {name}_plugin = self
        ctx.dispose(lambda: order.append("{name}"))

plugin = {make("one").strip().splitlines()[0] if False else ""}{""}
"""
        marker_one = tmp_path / "one.txt"
        marker_two = tmp_path / "two.txt"
        _write_plugin(tmp_path, "one", f"""
from pathlib import Path
class OnePlugin:
    name = "one"

    def apply(self, ctx, config=None):
        ctx.dispose(lambda: Path({str(marker_one)!r}).write_text("x"))

plugin = OnePlugin()
""")
        _write_plugin(tmp_path, "two", f"""
from pathlib import Path
class TwoPlugin:
    name = "two"

    def apply(self, ctx, config=None):
        ctx.dispose(lambda: Path({str(marker_two)!r}).write_text("x"))

plugin = TwoPlugin()
""")
        ctx = make_plugin_ctx(tmp_path)
        loader = Loader(ctx, tree=PluginTree.from_dict([
            {"id": "one", "name": "one"},
            {"id": "two", "name": "two"},
        ]))
        await loader.load()
        await loader.unload_all()
        assert marker_two.exists()  # reverse load order: two unloads first
        assert marker_one.exists()

    async def test_isolate_entry_scopes_services(self, tmp_path):
        _write_plugin(tmp_path, "provider", """
class ProviderPlugin:
    name = "provider"

    def apply(self, ctx, config=None):
        ctx.set("thing", "root-thing")

plugin = ProviderPlugin()
""")
        _write_plugin(tmp_path, "scoped_provider", """
class ScopedProviderPlugin:
    name = "scoped_provider"

    def apply(self, ctx, config=None):
        ctx.set("thing", "scoped-thing")

plugin = ScopedProviderPlugin()
""")
        ctx = make_plugin_ctx(tmp_path)
        loader = Loader(ctx, tree=PluginTree.from_dict([
            {"id": "provider", "name": "provider"},
            {"id": "scoped", "name": "scoped_provider", "isolate": {"thing": True}},
        ]))
        await loader.load()
        assert ctx.get("thing") == "root-thing"
        scoped_ctx = loader.handle("scoped")._fiber.ctx
        assert scoped_ctx.get("thing") == "scoped-thing"


class TestOrderIndependence:
    """Activation is service-availability driven: row order has no semantics."""

    @pytest.mark.asyncio
    async def test_shuffled_tree_mounts_all_plugins(self, tmp_path, monkeypatch):
        """A shuffled xcore.yaml still activates every plugin (inject-driven)."""
        import random

        from XBotv2.application import tree as application_tree
        from XBotv2.application.app import start_application
        from XBotv2.core.paths import RuntimePaths
        from XBotv2.llm.mock import MockLLM
        from XBotv2.loader import PluginTree

        # Shuffle the bundled tree (config preserved) into a temp yaml.
        tree = PluginTree.from_yaml(application_tree.DEFAULT_TREE).for_profile("agent")
        entries = list(tree.entries)
        random.Random(7).shuffle(entries)
        lines = []
        for entry in entries:
            lines.append(f"- id: {entry.id}\n  name: {entry.name}")
            if entry.config:
                cfg = yaml.safe_dump(entry.config, sort_keys=False).strip()
                lines.append(f"  config:\n" + "\n".join(
                    f"    {line}" for line in cfg.splitlines()
                ))
            if entry.disabled:
                lines.append("  disabled: true")
        shuffled = tmp_path / "shuffled.yaml"
        shuffled.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(application_tree, "DEFAULT_TREE", shuffled)

        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([{
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "mock",
                    "providers": {
                        "mock": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{"model": "mock"}],
                        },
                    },
                },
            }]),
            encoding="utf-8",
        )
        application = await start_application(
            paths=paths,
            workspace_root=tmp_path / "ws",
            provider_name="mock",
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )
        assert application.engine is not None
        assert "tools" in application.loader.loaded_ids
        assert "agent-runtime" in application.loader.loaded_ids
        assert "agentloop" in application.loader.loaded_ids
