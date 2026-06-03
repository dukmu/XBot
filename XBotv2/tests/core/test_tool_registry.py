"""Tests for ToolRegistry."""

import pytest
from langchain_core.tools import tool as langchain_tool

from xbotv2.tools.registry import ToolRegistry


@langchain_tool
def tool_a(x: int = 0) -> str:
    """Tool A."""
    return f"A: {x}"


@langchain_tool
def tool_b(y: str = "") -> str:
    """Tool B."""
    return f"B: {y}"


@langchain_tool
def filesystem_read(path: str) -> str:
    """Read a file."""
    return "content"


@langchain_tool
def filesystem_write(path: str, content: str) -> str:
    """Write a file."""
    return "ok"


@langchain_tool
def filesystem_list(path: str) -> str:
    """List files."""
    return "files"


class TestRegistration:
    """Tool registration."""

    def test_register_single_tool(self, tool_registry):
        """A tool can be registered."""
        tool_registry.register(tool_a, sandbox_mode="host")
        assert tool_registry.registered("tool_a")
        assert tool_registry.get("tool_a") is not None

    def test_register_with_metadata(self, tool_registry):
        """Registration stores metadata."""
        tool_registry.register(
            tool_a,
            sandbox_mode="sandboxed",
            execution_mode="parallel",
            lock_fields=("x",),
            owner_plugin="test_plugin",
        )
        entry = tool_registry.get("tool_a")
        assert entry.sandbox_mode == "sandboxed"
        assert entry.execution_mode == "parallel"
        assert entry.lock_fields == ("x",)
        assert entry.owner_plugin == "test_plugin"

    def test_register_many(self, tool_registry):
        """Batch registration."""
        tool_registry.register_many([tool_a, tool_b])
        assert tool_registry.registered("tool_a")
        assert tool_registry.registered("tool_b")
        assert len(tool_registry) == 2

    def test_register_many_with_mode_maps(self, tool_registry):
        """Batch registration with per-tool modes."""
        tool_registry.register_many(
            [tool_a, tool_b],
            sandbox_modes={"tool_a": "sandboxed", "tool_b": "host"},
            execution_modes={"tool_a": "parallel"},
        )
        assert tool_registry.get("tool_a").sandbox_mode == "sandboxed"
        assert tool_registry.get("tool_a").execution_mode == "parallel"
        assert tool_registry.get("tool_b").sandbox_mode == "host"

    def test_unregister_plugin_tools(self, tool_registry):
        """Plugin tools can be bulk-unregistered."""
        tool_registry.register(tool_a, owner_plugin="plugin_a")
        tool_registry.register(tool_b, owner_plugin="plugin_b")

        removed = tool_registry.unregister_plugin_tools("plugin_a")
        assert removed == ["tool_a"]
        assert not tool_registry.registered("tool_a")
        assert tool_registry.registered("tool_b")

    def test_unregister_nonexistent_plugin(self, tool_registry):
        """Unregistering a nonexistent plugin returns empty."""
        removed = tool_registry.unregister_plugin_tools("nonexistent")
        assert removed == []


class TestFiltering:
    """Tool filtering and wildcard expansion."""

    def test_filter_exact_names(self, tool_registry):
        """Filter by exact tool names."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)
        result = tool_registry.filter(["tool_a"])
        assert len(result) == 1
        assert result[0].name == "tool_a"

    def test_filter_all(self, tool_registry):
        """None or empty list returns all tools."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)
        assert len(tool_registry.filter(None)) == 2
        assert len(tool_registry.filter([])) == 2

    def test_filter_wildcard_expansion(self, tool_registry):
        """Wildcard suffix expands to all prefix-matching tools."""
        tool_registry.register(filesystem_read)
        tool_registry.register(filesystem_write)
        tool_registry.register(filesystem_list)
        tool_registry.register(tool_a)

        result = tool_registry.filter(["filesystem*"])
        names = {t.name for t in result}
        assert names == {"filesystem_read", "filesystem_write", "filesystem_list"}

    def test_filter_prefix_expansion(self, tool_registry):
        """Bare prefix expands to all matching tools (like current XBot)."""
        tool_registry.register(filesystem_read)
        tool_registry.register(filesystem_write)
        tool_registry.register(filesystem_list)
        tool_registry.register(tool_a)

        result = tool_registry.filter(["filesystem"])
        names = {t.name for t in result}
        assert names == {"filesystem_read", "filesystem_write", "filesystem_list"}

    def test_filter_nonexistent_tool(self, tool_registry):
        """Filtering nonexistent tools returns empty."""
        result = tool_registry.filter(["nonexistent"])
        assert len(result) == 0

    def test_restrict_limits_visible_and_executable_tools(self, tool_registry):
        """Restrict changes registry visibility, unlike pure filter()."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)

        enabled = tool_registry.restrict(["tool_a"])

        assert enabled == ["tool_a"]
        assert tool_registry.names() == ["tool_a"]
        assert [tool.name for tool in tool_registry.get_all()] == ["tool_a"]
        assert tool_registry.get("tool_a") is not None
        assert tool_registry.get("tool_b") is None

    def test_restrict_expands_prefix_and_rejects_unknown(self, tool_registry):
        """Restrict supports group selectors but fails on unknown selectors."""
        tool_registry.register(filesystem_read)
        tool_registry.register(filesystem_write)
        tool_registry.register(tool_a)

        tool_registry.restrict(["filesystem"])
        assert set(tool_registry.names()) == {"filesystem_read", "filesystem_write"}

        with pytest.raises(ValueError, match="Unknown tool selector"):
            tool_registry.restrict(["missing"])


class TestQuery:
    """Query methods."""

    def test_names(self, tool_registry):
        """List all registered names."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)
        assert set(tool_registry.names()) == {"tool_a", "tool_b"}

    def test_get_all(self, tool_registry):
        """Get all tool instances."""
        tool_registry.register(tool_a)
        tools = tool_registry.get_all()
        assert len(tools) == 1
        assert tools[0].name == "tool_a"

    def test_sandbox_modes_dict(self, tool_registry):
        """Get all sandbox modes as a dict."""
        tool_registry.register(tool_a, sandbox_mode="host")
        tool_registry.register(tool_b, sandbox_mode="sandboxed")
        modes = tool_registry.sandbox_modes()
        assert modes == {"tool_a": "host", "tool_b": "sandboxed"}

    def test_len_and_contains(self, tool_registry):
        """__len__ and __contains__ work."""
        tool_registry.register(tool_a)
        assert len(tool_registry) == 1
        assert "tool_a" in tool_registry
        assert "nonexistent" not in tool_registry
