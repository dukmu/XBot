"""Tests for TokenManagerPlugin — estimator, stats, budget, and hook integration."""

import pytest

from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall


class TestTokenEstimator:
    def test_estimate_tokens_chars_div_4(self):
        from builtin_plugins.token_manager.estimator import estimate_tokens

        assert estimate_tokens("hello world") == 2  # 11/4
        assert estimate_tokens("") == 1  # min
        assert estimate_tokens("x" * 400) == 100

    def test_estimate_message_tokens(self):
        from builtin_plugins.token_manager.estimator import estimate_message_tokens

        msg = Message(
            role="assistant",
            content="hello",
            tool_calls=[ToolCall("call_1", "shell", {"cmd": "ls"})],
        )
        tokens = estimate_message_tokens(msg)
        assert tokens >= 2  # content + tool name + args

    def test_estimate_context_tokens(self):
        from builtin_plugins.token_manager.estimator import estimate_context_tokens

        msgs = [
            Message(role="system", content="x" * 40),
            Message(role="user", content="y" * 20),
        ]
        tokens = estimate_context_tokens(msgs)
        assert tokens == 15  # 40/4 + 20/4 = 10 + 5

    def test_estimate_tool_schema_tokens(self):
        from builtin_plugins.token_manager.estimator import estimate_tool_schema_tokens
        from xbotv2.api.tools import Tool

        def echo(msg: str) -> str:
            """Echo a message back."""
            return msg

        tool = Tool.from_function(echo, name="echo")
        tokens = estimate_tool_schema_tokens([tool])
        assert tokens > 0


class TestTokenStatsCollector:
    def test_start_and_finish_turn(self):
        from builtin_plugins.token_manager.stats import TokenStatsCollector

        s = TokenStatsCollector()
        s.start_turn(1, provider="test", model="x")
        s.record_usage(100, 50, cache_hit=200, cache_miss=30)
        s.record_tool_call()
        t = s.finish_turn()

        assert t.prompt_tokens == 100
        assert t.completion_tokens == 50
        assert t.cache_hit_tokens == 200
        assert t.cache_miss_tokens == 30
        assert t.tool_calls == 1
        assert s.summary()["cumulative_prompt_tokens"] == 100
        assert s.summary()["cumulative_completion_tokens"] == 50
        assert s.summary()["cumulative_cache_hit_tokens"] == 200

    def test_multiple_turns_cumulative(self):
        from builtin_plugins.token_manager.stats import TokenStatsCollector

        s = TokenStatsCollector()
        s.start_turn(1); s.record_usage(100, 50); s.finish_turn()
        s.start_turn(2); s.record_usage(200, 80); s.finish_turn()

        assert s.summary()["cumulative_prompt_tokens"] == 300
        assert s.summary()["cumulative_completion_tokens"] == 130
        assert len(s.turns) == 2

    def test_summary(self):
        from builtin_plugins.token_manager.stats import TokenStatsCollector

        s = TokenStatsCollector()
        s.start_turn(1); s.record_usage(100, 50); s.finish_turn()

        summary = s.summary()
        assert summary["turns"] == 1
        assert summary["cumulative_prompt_tokens"] == 100
        assert summary["last_turn"]["turn"] == 1
        assert summary["last_turn"]["prompt_tokens"] == 100

    def test_summary_with_active_turn(self):
        from builtin_plugins.token_manager.stats import TokenStatsCollector

        s = TokenStatsCollector()
        s.start_turn(1)
        s.record_usage(100, 50)

        summary = s.summary()
        assert summary["last_turn"] is not None
        assert summary["last_turn"]["turn"] == 1
        assert summary["last_turn"]["prompt_tokens"] == 100

    def test_context_update_and_reset(self):
        from builtin_plugins.token_manager.stats import TokenStatsCollector

        stats = TokenStatsCollector()
        stats.start_turn(3)
        stats.update_context(estimated_prompt=120, message_count=7)

        assert stats.summary()["last_turn"]["estimated_prompt"] == 120
        assert stats.summary()["last_turn"]["context_messages"] == 7

        stats.reset()
        assert stats.summary()["turns"] == 0
        assert stats.summary()["last_turn"] is None

    def test_plugin_diagnostics_expose_health_and_usage(self):
        from builtin_plugins.token_manager.plugin import TokenManagerPlugin
        from xbotv2.api import PluginManifest

        plugin = TokenManagerPlugin(
            PluginManifest(name="token_manager", version="1"),
            store=None,
        )

        diagnostics = plugin.diagnostics()

        assert diagnostics["status"] == "ready"
        assert diagnostics["usage"]["turns"] == 0

    @pytest.mark.asyncio
    async def test_plugin_unload_resets_in_memory_stats(self):
        from builtin_plugins.token_manager.plugin import TokenManagerPlugin
        from xbotv2.api import PluginManifest

        plugin = TokenManagerPlugin(
            PluginManifest(name="token_manager", version="1"),
            store=None,
        )
        plugin._stats.start_turn(1)
        plugin._stats.record_usage(10, 5)
        plugin._stats.finish_turn()

        await plugin.on_unload()

        assert plugin.summary()["turns"] == 0
        assert plugin.summary()["last_turn"] is None

    @pytest.mark.asyncio
    async def test_plugin_reads_public_model_request(self):
        from builtin_plugins.token_manager.plugin import TokenManagerPlugin
        from xbotv2.api import HookContext, HookStage, PluginManifest, Tool

        def echo(value: str) -> str:
            return value

        plugin = TokenManagerPlugin(
            PluginManifest(name="token_manager", version="1"),
            store=None,
        )
        plugin._stats.start_turn(1)
        ctx = HookContext(
            stage=HookStage.BEFORE_MODEL_REQUEST,
            model_request={
                "messages": [Message(role="user", content="hello")],
                "tools": [Tool.from_function(echo)],
                "llm": object(),
            },
        )

        await plugin._on_before_model_request(ctx)

        current = plugin.summary()["last_turn"]
        assert current["context_messages"] == 1
        assert current["estimated_prompt"] > 0


class TestTokenBudgetController:
    def test_ok_under_soft_limit(self):
        from builtin_plugins.token_manager.budget import TokenBudgetController

        budget = TokenBudgetController(max_context_tokens=1000, output_reservation=100)
        msgs = [Message(role="user", content="hello")]
        check = budget.check_context(msgs)
        assert check["action"] == "ok"

    def test_soft_limit_exceeded(self):
        from builtin_plugins.token_manager.budget import TokenBudgetController

        budget = TokenBudgetController(max_context_tokens=100, output_reservation=20)
        # hard=80, soft=64. x*300 chars = 75 tokens > soft, < hard
        msgs = [Message(role="system", content="x" * 300)]
        check = budget.check_context(msgs)
        assert check["action"] == "soft_limit_exceeded"

    def test_hard_limit_exceeded(self):
        from builtin_plugins.token_manager.budget import TokenBudgetController

        budget = TokenBudgetController(max_context_tokens=100, output_reservation=20)
        # hard=80. x*400 chars = 100 tokens > hard
        msgs = [Message(role="system", content="x" * 400)]
        check = budget.check_context(msgs)
        assert check["action"] == "hard_limit_exceeded"

    def test_tool_schema_included(self):
        from builtin_plugins.token_manager.budget import TokenBudgetController
        from xbotv2.api.tools import Tool

        def big_tool(very_long_param_name: str, another_param: str) -> str:
            """A tool with a long description """ + "x" * 500
            return "ok"

        budget = TokenBudgetController(max_context_tokens=1000, output_reservation=100)
        tool = Tool.from_function(big_tool, name="big_tool")
        msgs = [Message(role="user", content="hello")]
        check = budget.check_context(msgs, tools=[tool])
        assert check["tool_schema_tokens"] > 0
        assert check["total_estimated"] > check["estimated_tokens"]
