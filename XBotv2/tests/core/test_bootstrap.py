"""Tests for the bootstrap sequence — engine with zero plugins."""

import pytest

from xbotv2.core.bootstrap import bootstrap
from xbotv2.llm.mock import MockLLM


class TestBootstrapBasics:
    """Minimal bootstrap without plugins."""

    @pytest.mark.asyncio
    async def test_bootstrap_creates_engine(self, temp_data_dir):
        """Bootstrap returns a working engine."""
        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            personality_id="default",
            llm_override=MockLLM(responses=[{"content": "Hello!"}]),
        )
        assert engine is not None
        assert engine.turn_count == 0

    @pytest.mark.asyncio
    async def test_bootstrap_registers_core_tools(self, temp_data_dir):
        """Core base tools are always registered."""
        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            llm_override=MockLLM(responses=[]),
        )
        tool_names = engine.tool_registry.names()
        # Core tools always present
        assert "shell" in tool_names
        assert "filesystem_read" in tool_names
        assert "filesystem_write" in tool_names
        assert "filesystem_list" in tool_names
        assert "ask" not in tool_names

    @pytest.mark.asyncio
    async def test_bootstrap_tool_filter_limits_visible_tools(self, temp_data_dir):
        """Personality tool selectors restrict tools passed to the model."""
        personality = temp_data_dir / "personalities" / "default" / "personality.yaml"
        personality.write_text("tools:\n  - filesystem_read\n")

        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            llm_override=MockLLM(responses=[]),
        )

        assert engine.tool_registry.names() == ["filesystem_read"]
        assert [tool.name for tool in engine.tool_registry.get_all()] == ["filesystem_read"]

    @pytest.mark.asyncio
    async def test_bootstrap_unknown_tool_filter_raises(self, temp_data_dir):
        """Unknown tool selectors fail closed instead of exposing all tools."""
        personality = temp_data_dir / "personalities" / "default" / "personality.yaml"
        personality.write_text("tools:\n  - no_such_tool\n")

        with pytest.raises(ValueError, match="Unknown tool selector"):
            await bootstrap(
                config_dir=str(temp_data_dir),
                session_id="test-session",
                thread_id="test-thread",
                llm_override=MockLLM(responses=[]),
            )

    @pytest.mark.asyncio
    async def test_bootstrap_engine_runs_turn(self, temp_data_dir, temp_workspace):
        """Engine from bootstrap can run a turn."""
        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            llm_override=MockLLM(responses=[{"content": "Hello from bootstrap!"}]),
        )
        # Override workspace for the sandbox
        engine.sandbox_policy.workspace_root = temp_workspace

        events = [e async for e in engine.run_turn("hi")]
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 1
        assert "Hello from bootstrap!" in assistant_events[0]["data"]["content"]

    @pytest.mark.asyncio
    async def test_bootstrap_creates_state(self, temp_data_dir):
        """Bootstrap creates the state store with events."""
        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            llm_override=MockLLM(responses=[]),
        )
        state = engine.state_store.read_state()
        assert state["session_id"] == "test-session"
        assert state["thread_id"] == "test-thread"
        assert state["schema_version"] == 2


class TestBootstrapNoPlugins:
    """Engine works correctly with zero plugins."""

    @pytest.mark.asyncio
    async def test_engine_without_plugins_works(self, temp_data_dir, temp_workspace):
        """Core engine with no plugins runs ReAct correctly."""
        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[],  # Explicitly no plugin dirs
            llm_override=MockLLM(responses=[{"content": "I work without plugins!"}]),
        )
        engine.sandbox_policy.workspace_root = temp_workspace

        events = [e async for e in engine.run_turn("test")]
        types = [e["type"] for e in events]
        assert "turn_started" in types
        assert "assistant_message" in types
        assert "turn_finished" in types
