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

    def test_duplicate_key_is_rejected_without_replacing_original(self, tool_registry):
        tool_registry.register(tool_a)
        original = tool_registry.get("tool_a")

        with pytest.raises(ValueError, match="already registered"):
            tool_registry.register(tool_a)

        assert tool_registry.get("tool_a") is original

    def test_duplicate_display_name_is_rejected_across_namespaces(
        self, tool_registry
    ):
        tool_registry.register(tool_a, namespace="plugin:first")

        with pytest.raises(ValueError, match="Tool name 'tool_a'"):
            tool_registry.register(tool_a, namespace="plugin:second")

        assert tool_registry.registered_names() == ["plugin:first:tool_a"]

    def test_register_with_metadata(self, tool_registry):
        """Registration stores metadata."""
        tool_registry.register(
            tool_a,
            sandbox_mode="sandboxed",
        )
        entry = tool_registry.get("tool_a")
        assert entry.sandbox_mode == "sandboxed"
        assert entry.registered_name == "tool_a"

    def test_register_namespaced_tool_identity(self, tool_registry):
        """Namespaced registration produces unique canonical names."""
        tool_registry.register(
            tool_a,
            namespace="plugin:skills",
        )
        tool_registry.register(tool_b, namespace="skills:global")
        tool_registry.register(filesystem_read, namespace="mcp:github")

        plugin_entry = tool_registry.get("plugin:skills:tool_a")
        skill_entry = tool_registry.get("skills:global:tool_b")
        mcp_entry = tool_registry.get("mcp:github:filesystem_read")

        assert plugin_entry.registered_name == "plugin:skills:tool_a"
        assert skill_entry.registered_name == "skills:global:tool_b"
        assert mcp_entry.registered_name == "mcp:github:filesystem_read"
        assert set(tool_registry.registered_names()) == {
            "plugin:skills:tool_a",
            "skills:global:tool_b",
            "mcp:github:filesystem_read",
        }

    def test_unregister_single_tool(self, tool_registry):
        """A single tool can be unregistered by name."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)
        tool_registry.restrict(["tool_a", "tool_b"])

        assert tool_registry.unregister("tool_a") is True
        assert tool_registry.unregister("tool_a") is False
        assert not tool_registry.registered("tool_a")
        assert tool_registry.names() == ["tool_b"]


class TestFiltering:
    """Tool restriction and wildcard expansion."""

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

    def test_empty_restriction_exposes_no_tools(self, tool_registry):
        tool_registry.register(tool_a)

        assert tool_registry.restrict([]) == []
        assert tool_registry.get_all() == []

    def test_restrict_expands_prefix_and_silently_ignores_unmatched(self, tool_registry):
        """Restrict supports group selectors; unmatched selectors are silently ignored."""
        tool_registry.register(filesystem_read)
        tool_registry.register(filesystem_write)
        tool_registry.register(tool_a)

        tool_registry.restrict(["filesystem"])
        assert set(tool_registry.names()) == {"filesystem_read", "filesystem_write"}

        tool_registry.restrict(["missing"])
        assert tool_registry.names() == []

    def test_exclude_removes_selectors_from_current_tools(self, tool_registry):
        tool_registry.register(filesystem_read)
        tool_registry.register(filesystem_write)
        tool_registry.register(tool_a)

        assert set(tool_registry.exclude(["filesystem_write"])) == {
            "filesystem_read",
            "tool_a",
        }
        assert tool_registry.get("filesystem_write") is None


class TestQuery:
    """Query methods."""

    def test_names(self, tool_registry):
        """List all registered names."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)
        assert set(tool_registry.names()) == {"tool_a", "tool_b"}

    def test_registered_names_ignores_restrictions(self, tool_registry):
        """Introspection can see hidden registered tools."""
        tool_registry.register(tool_a)
        tool_registry.register(tool_b)
        tool_registry.restrict(["tool_a"])

        assert tool_registry.names() == ["tool_a"]
        assert set(tool_registry.registered_names()) == {"tool_a", "tool_b"}

    def test_registered_entries_preserve_registration_order(self, tool_registry):
        tool_registry.register(tool_a)
        tool_registry.register(tool_b, namespace="skills:global")

        entries = tool_registry.registered_entries()

        assert isinstance(entries, tuple)
        assert [entry.registered_name for entry in entries] == [
            "tool_a",
            "skills:global:tool_b",
        ]

    def test_model_hidden_tool_requires_explicit_lookup(self, tool_registry):
        tool_registry.register(tool_a, model_visible=False)

        assert tool_registry.get("tool_a") is None
        assert tool_registry.get_registered("tool_a") is not None
        assert tool_registry.get_all() == []

    def test_get_all(self, tool_registry):
        """Get all tool instances."""
        tool_registry.register(tool_a)
        tools = tool_registry.get_all()
        assert len(tools) == 1
        assert tools[0].name == "tool_a"

    def test_len_and_contains(self, tool_registry):
        """__len__ and __contains__ work."""
        tool_registry.register(tool_a)
        assert len(tool_registry) == 1
        assert "tool_a" in tool_registry
        assert "nonexistent" not in tool_registry
