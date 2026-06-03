"""Tests for ContextBuilder — fragment injection and caching."""

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from xbotv2.core.context import ContextBuilder


class TestContextBuilderBasics:
    """Basic message assembly."""

    def test_build_minimal_context(self, context_builder):
        """Build context with only required fields."""
        messages = context_builder.build(
            messages=[HumanMessage(content="hello")],
            agent_name="TestBot",
            user_name="tester",
        )
        assert len(messages) > 0
        # First message is system prefix
        assert isinstance(messages[0], SystemMessage)
        assert "TestBot" in messages[0].content

    def test_build_includes_history(self, context_builder):
        """History messages appear after system messages."""
        messages = context_builder.build(
            messages=[
                HumanMessage(content="hello"),
                HumanMessage(content="world"),
            ],
            agent_name="TestBot",
        )
        # Find the human messages
        human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 2

    def test_build_ends_with_current_state(self, context_builder):
        """The last message is always the current state suffix."""
        messages = context_builder.build(
            messages=[],
            agent_name="TestBot",
            turn_count=5,
        )
        last = messages[-1]
        assert isinstance(last, SystemMessage)
        assert "Current State" in last.content
        assert "Turn: 6" in last.content  # turn_count + 1

    def test_build_includes_runtime_rules(self, context_builder):
        """Runtime rules section is always present."""
        messages = context_builder.build(messages=[], agent_name="TestBot")
        rules_text = "\n".join(
            m.content for m in messages if "Runtime Rules" in m.content
        )
        assert "Always use tools" in rules_text
        assert "Never invent file contents" in rules_text


class TestFragmentInjection:
    """Plugin fragment injection into context."""

    def test_register_fragment_system_instructions(self, context_builder):
        """Fragments at system_instructions appear after the prefix."""
        context_builder.register_fragment(
            "system_instructions", "test_plugin", "## Test Instructions\nBe helpful."
        )
        messages = context_builder.build(messages=[], agent_name="TestBot")
        found = [m for m in messages if "Test Instructions" in m.content]
        assert len(found) == 1

    def test_register_fragment_dag_suffix(self, context_builder):
        """Fragments at dag_suffix appear before current state."""
        context_builder.register_fragment(
            "dag_suffix", "planning_plugin", "## Plan Status\nActive: node-1"
        )
        messages = context_builder.build(messages=[], agent_name="TestBot")
        # Find the suffix section
        suffix_idx = None
        for i, m in enumerate(messages):
            if "Current State" in m.content:
                suffix_idx = i
                break
        assert suffix_idx is not None
        assert "Plan Status" in messages[suffix_idx].content

    def test_register_fragment_invalid_stage_raises(self, context_builder):
        """Invalid fragment stages raise ValueError."""
        with pytest.raises(ValueError, match="Unknown fragment stage"):
            context_builder.register_fragment("nonexistent", "p", "text")

    def test_unregister_fragment(self, context_builder):
        """Fragments can be removed."""
        context_builder.register_fragment(
            "system_instructions", "test_plugin", "## Remove Me"
        )
        context_builder.unregister_fragment("system_instructions", "test_plugin")
        messages = context_builder.build(messages=[], agent_name="TestBot")
        found = [m for m in messages if "Remove Me" in m.content]
        assert len(found) == 0

    def test_multiple_plugins_same_stage(self, context_builder):
        """Multiple plugins can inject at the same stage."""
        context_builder.register_fragment(
            "dag_suffix", "plugin_a", "## Plugin A"
        )
        context_builder.register_fragment(
            "dag_suffix", "plugin_b", "## Plugin B"
        )
        messages = context_builder.build(messages=[], agent_name="TestBot")
        suffix_idx = None
        for i, m in enumerate(messages):
            if "Current State" in m.content:
                suffix_idx = i
                break
        content = messages[suffix_idx].content
        assert "Plugin A" in content
        assert "Plugin B" in content

    def test_empty_fragments_arent_injected(self, context_builder):
        """Empty text fragments are skipped."""
        context_builder.register_fragment("system_instructions", "p", "")
        context_builder.register_fragment("system_instructions", "q", "\n  ")
        messages = context_builder.build(messages=[], agent_name="TestBot")
        # Should still build normally
        assert len(messages) > 0


class TestCacheIsolation:
    """System prefix caching is instance-level, not module-level."""

    def test_caches_are_isolated(self):
        """Each ContextBuilder has its own cache."""
        cb1 = ContextBuilder()
        cb2 = ContextBuilder()

        cb1.register_fragment("system_prefix", "p1", "data1")
        cb2.register_fragment("system_prefix", "p2", "data2")

        messages1 = cb1.build(messages=[], agent_name="TestBot")
        messages2 = cb2.build(messages=[], agent_name="TestBot")

        # cb1 should have p1, cb2 should have p2
        prefix1 = messages1[0].content
        prefix2 = messages2[0].content
        assert "data1" in prefix1
        assert "data1" not in prefix2
        assert "data2" in prefix2
        assert "data2" not in prefix1

    def test_cache_invalidation(self, context_builder):
        """Adding a fragment invalidates the cache."""
        messages1 = context_builder.build(messages=[], agent_name="TestBot")
        prefix1 = messages1[0].content

        context_builder.register_fragment("system_prefix", "p", "NEW DATA")
        messages2 = context_builder.build(messages=[], agent_name="TestBot")
        prefix2 = messages2[0].content

        assert prefix1 != prefix2
        assert "NEW DATA" in prefix2


class TestSanitization:
    """Message history sanitization."""

    def test_drops_orphan_tool_messages(self, context_builder):
        """Orphan ToolMessages (no matching AIMessage) are removed."""
        from langchain_core.messages import AIMessage, ToolMessage

        # AIMessage with a tool call, then a ToolMessage for a different ID
        messages = [
            AIMessage(
                content="test",
                tool_calls=[{"name": "shell", "args": {}, "id": "call_1"}],
            ),
            ToolMessage(content="result", tool_call_id="call_2"),  # orphan
        ]
        sanitized = context_builder._sanitize_history(messages)
        tool_msgs = [m for m in sanitized if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 0

    def test_keeps_valid_tool_messages(self, context_builder):
        """Valid ToolMessages (matching AIMessage) are kept."""
        from langchain_core.messages import AIMessage, ToolMessage

        messages = [
            AIMessage(
                content="test",
                tool_calls=[{"name": "shell", "args": {}, "id": "call_1"}],
            ),
            ToolMessage(content="result", tool_call_id="call_1"),
        ]
        sanitized = context_builder._sanitize_history(messages)
        tool_msgs = [m for m in sanitized if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
