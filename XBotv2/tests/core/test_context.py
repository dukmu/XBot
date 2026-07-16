"""Tests for ContextBuilder — fragment injection and caching."""

import pytest

from xbotv2.core.context import ContextBuilder
from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall


class TestContextBuilderBasics:
    """Basic message assembly."""

    def test_build_minimal_context(self, context_builder):
        """Build context with only required fields."""
        messages = context_builder.build(
            messages=[Message(role="user", content="hello")],
            agent_name="TestBot",
            user_name="tester",
        )
        assert len(messages) > 0
        # First message is system prefix
        assert messages[0].role == "system"
        assert "TestBot" in messages[0].content

    def test_build_includes_history(self, context_builder):
        """History messages appear after system messages."""
        messages = context_builder.build(
            messages=[
                Message(role="user", content="hello"),
                Message(role="user", content="world"),
            ],
            agent_name="TestBot",
        )
        # Find the human messages
        human_msgs = [m for m in messages if m.role == "user"]
        assert len(human_msgs) == 2

    def test_default_system_prompt_is_stable_between_builds(self, context_builder):
        first = context_builder.build(
            messages=[],
            agent_name="TestBot",
            turn_count=5,
        )
        second = context_builder.build(
            messages=[],
            agent_name="TestBot",
            turn_count=5,
        )

        assert first[0].role == "system"
        assert first[0].content == second[0].content
        assert "Current State" not in first[0].content
        assert "Time:" not in first[0].content

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

    def test_register_fragment_context_suffix(self, context_builder):
        """Fragments at context_suffix appear before current state."""
        context_builder.register_fragment(
            "context_suffix", "planning_plugin", "## Plan Status\nActive: node-1"
        )
        messages = context_builder.build(messages=[], agent_name="TestBot")
        assert "Plan Status" in messages[0].content

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
            "context_suffix", "plugin_a", "## Plugin A"
        )
        context_builder.register_fragment(
            "context_suffix", "plugin_b", "## Plugin B"
        )
        messages = context_builder.build(messages=[], agent_name="TestBot")
        content = messages[0].content
        assert "Plugin A" in content
        assert "Plugin B" in content

    def test_empty_fragments_arent_injected(self, context_builder):
        """Empty text fragments are skipped."""
        context_builder.register_fragment("system_instructions", "p", "")
        context_builder.register_fragment("system_instructions", "q", "\n  ")
        messages = context_builder.build(messages=[], agent_name="TestBot")
        # Should still build normally
        assert len(messages) > 0


class TestContextComponents:
    """Source-tagged context components for token accounting."""

    def test_build_components_preserves_source_and_owner_metadata(self, context_builder):
        context_builder.register_fragment(
            "system_instructions", "skills", "## Skills\nUse skills."
        )
        context_builder.register_fragment(
            "system_rules", "compact", "## Compact\nStay small."
        )
        components = context_builder.build_components(
            messages=[Message(role="user", content="hello")],
            agent_name="TestBot",
        )

        sources = [component.source for component in components]
        assert sources[:3] == [
            "system_prefix",
            "plugin_fragment",
            "runtime_rules",
        ]
        assert "history" in sources
        assert sources[-1] == "history"

        skills = next(
            component
            for component in components
            if component.plugin_name == "skills"
        )
        assert skills.stage == "system_instructions"
        assert "Skills" in skills.content

        compact = next(
            component
            for component in components
            if component.plugin_name == "compact"
        )
        assert compact.stage == "system_rules"
        assert "Compact" in compact.content

    def test_context_suffix_preserves_stage_and_owner_metadata(self, context_builder):
        context_builder.register_fragment(
            "context_suffix",
            "status",
            "## Runtime Status\nReady.",
        )

        components = context_builder.build_components(messages=[])

        suffix = components[-1]
        assert suffix.source == "context_suffix"
        assert suffix.stage == "context_suffix"
        assert suffix.plugin_name == "status"
        assert "Runtime Status" in suffix.content

    def test_messages_from_components_roundtrips_to_build_shape(self, context_builder):
        raw_messages = [Message(role="user", content="hello")]
        direct = context_builder.build(messages=raw_messages, agent_name="TestBot")
        via_components = context_builder.messages_from_components(
            context_builder.build_components(messages=raw_messages, agent_name="TestBot")
        )

        assert [type(message) for message in via_components] == [
            type(message) for message in direct
        ]
        assert [message.role for message in via_components] == [
            message.role for message in direct
        ]
        assert "Runtime Rules" in via_components[0].content
        assert "Runtime Rules" in direct[0].content
        assert via_components[-1].content == direct[-1].content == "hello"
        assert "Current State" not in via_components[0].content
        assert "Current State" not in direct[0].content

    def test_active_subagents_add_only_needed_dynamic_state(self, context_builder):
        messages = context_builder.build(
            messages=[],
            active_subagents=2,
        )

        assert "# Current State" in messages[0].content
        assert "Active subagents: 2" in messages[0].content
        assert "Time:" not in messages[0].content

    def test_messages_from_components_rejects_untyped_values(self, context_builder):
        with pytest.raises(TypeError, match="must be a ContextComponent"):
            context_builder.messages_from_components([object()])


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
        messages = [
            Message(
                role="assistant",
                content="test",
                tool_calls=[ToolCall("call_1", "shell", {})],
            ),
            Message(role="tool", content="result", tool_call_id="call_2"),
        ]
        sanitized = context_builder._sanitize_history(messages)
        tool_msgs = [m for m in sanitized if m.role == "tool"]
        assert len(tool_msgs) == 0

    def test_keeps_valid_tool_messages(self, context_builder):
        messages = [
            Message(
                role="assistant",
                content="test",
                tool_calls=[ToolCall("call_1", "shell", {})],
            ),
            Message(role="tool", content="result", tool_call_id="call_1"),
        ]
        sanitized = context_builder._sanitize_history(messages)
        tool_msgs = [m for m in sanitized if m.role == "tool"]
        assert len(tool_msgs) == 1
