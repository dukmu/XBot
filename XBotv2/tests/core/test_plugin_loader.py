"""Tests for PluginLoader — discovery, dependency resolution, loading."""

import tempfile
from pathlib import Path

import pytest
import yaml

from xbotv2.plugin.manifest import PluginManifest
from xbotv2.core.bootstrap import _resolve_dependencies


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
        result = _resolve_dependencies(items)
        names = [m.name for m, _ in result]
        assert names == ["a", "b", "c"]

    def test_simple_dependency(self):
        """A depends on B → B comes before A."""
        items = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b"),
        ]
        result = _resolve_dependencies(items)
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
        result = _resolve_dependencies(items)
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
            _resolve_dependencies(items)

    def test_circular_dependency_raises(self):
        """A→B, B→A raises."""
        items = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b", deps=["a"]),
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            _resolve_dependencies(items)

    def test_chain_dependency(self):
        """Long chain: a→b→c→d."""
        items = [
            _make_manifest_tuple("a", deps=["b"]),
            _make_manifest_tuple("b", deps=["c"]),
            _make_manifest_tuple("c", deps=["d"]),
            _make_manifest_tuple("d"),
        ]
        result = _resolve_dependencies(items)
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
