"""Tests for the bootstrap sequence — engine with zero plugins."""

import json

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
    async def test_bootstrap_rejects_path_like_identifiers(self, temp_data_dir, tmp_path):
        """Runtime identifiers cannot escape the configured data directory."""
        with pytest.raises(ValueError, match="session_id"):
            await bootstrap(
                config_dir=str(temp_data_dir),
                session_id="../escape",
                thread_id="test-thread",
                personality_id="default",
                llm_override=MockLLM(responses=[]),
            )

        assert not (tmp_path / "escape").exists()

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
        assert "send_message" in tool_names
        assert "ask_user" in tool_names
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
    async def test_bootstrap_registers_personality_hooks(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """Personality-declared hooks are resolved and registered."""
        hook_dir = tmp_path / "hook_modules"
        hook_dir.mkdir()
        (hook_dir / "test_personality_hooks.py").write_text(
            """
async def before_user_message(ctx):
    return {"user_input": ctx.user_input + " from hook"}
"""
        )
        monkeypatch.syspath_prepend(str(hook_dir))

        personality = temp_data_dir / "personalities" / "default" / "personality.yaml"
        personality.write_text(
            """
hooks:
  - stage: before_user_message_accept
    target: test_personality_hooks:before_user_message
"""
        )

        engine = await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            llm_override=MockLLM(responses=[{"content": "ok"}]),
        )

        events = [e async for e in engine.run_turn("hello")]

        assert events[-1]["type"] == "turn_finished"
        assert engine.messages[0].content == "hello from hook"

    @pytest.mark.asyncio
    async def test_bootstrap_invalid_personality_hook_raises(self, temp_data_dir):
        """Broken personality hook declarations fail loudly."""
        personality = temp_data_dir / "personalities" / "default" / "personality.yaml"
        personality.write_text(
            """
hooks:
  - stage: on_turn_start
    target: missing_module:nope
"""
        )

        with pytest.raises(ModuleNotFoundError):
            await bootstrap(
                config_dir=str(temp_data_dir),
                session_id="test-session",
                thread_id="test-thread",
                llm_override=MockLLM(responses=[]),
            )

    @pytest.mark.asyncio
    async def test_bootstrap_passes_external_plugin_configs(
        self, temp_data_dir, tmp_path, monkeypatch
    ):
        """External bootstrap plugin_configs reach plugin on_load."""
        plugin_root = tmp_path / "plugins"
        plugin_dir = plugin_root / "configured"
        plugin_dir.mkdir(parents=True)
        output_path = tmp_path / "received.json"
        (plugin_dir / "plugin.yaml").write_text(
            """
name: configured
version: 0.1.0
"""
        )
        (plugin_dir / "__init__.py").write_text(
            f"""
import json
from xbotv2.plugin.base import PluginBase

class ConfiguredPlugin(PluginBase):
    async def on_load(self, config=None):
        with open({str(output_path)!r}, "w", encoding="utf-8") as fh:
            json.dump(config or {{}}, fh, sort_keys=True)
"""
        )
        monkeypatch.syspath_prepend(str(plugin_dir))

        await bootstrap(
            config_dir=str(temp_data_dir),
            session_id="test-session",
            thread_id="test-thread",
            plugin_dirs=[plugin_root],
            plugin_configs={"configured": {"value": 42}},
            llm_override=MockLLM(responses=[]),
        )

        assert json.loads(output_path.read_text(encoding="utf-8")) == {"value": 42}

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
