"""TokenManagerPlugin — token estimation, usage tracking, and budget control."""

from __future__ import annotations

from typing import Any

from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.plugin.base import PluginBase
from xbotv2.plugin.manifest import PluginManifest
from xbotv2.plugin.store import PluginStore

from .budget import TokenBudgetController
from .estimator import estimate_context_tokens, estimate_tool_schema_tokens
from .stats import TokenStatsCollector

import logging

logger = logging.getLogger("xbotv2.token_manager")


class TokenManagerPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._stats = TokenStatsCollector()
        self._budget = TokenBudgetController(
            max_context_tokens=32000,
            output_reservation=4096,
            soft_limit_ratio=0.8,
        )

    async def on_load(self, config: dict[str, Any]) -> None:
        if config:
            self._budget = TokenBudgetController(
                max_context_tokens=int(config.get("max_context_tokens", 32000)),
                output_reservation=int(config.get("output_reservation", 4096)),
                soft_limit_ratio=float(config.get("soft_limit_ratio", 0.8)),
            )

    def register_hooks(self, manager: HookManager) -> None:
        manager.register(HookStage.ON_TURN_START, self._on_turn_start)
        manager.register(HookStage.BEFORE_MODEL_REQUEST, self._on_before_model_request)
        manager.register(HookStage.AFTER_MODEL_RESPONSE, self._on_after_model_response)
        manager.register(HookStage.ON_TOOL_CALLS_PARSED, self._on_tool_calls_parsed)
        manager.register(HookStage.ON_TURN_END, self._on_turn_end)
        manager.register(HookStage.BEFORE_STATE_PERSIST, self._on_before_state_persist)

    async def _on_turn_start(self, ctx: HookContext) -> None:
        self._stats.start_turn(
            turn=int(getattr(ctx, "turn_count", 0) or 0),
        )

    async def _on_before_model_request(self, ctx: HookContext) -> None:
        msgs = ctx.model_request.get("messages", []) if ctx.model_request else []
        tools = ctx.model_request.get("tools", []) if ctx.model_request else []
        estimated = estimate_context_tokens(msgs)
        tool_tokens = estimate_tool_schema_tokens(tools)
        self._stats._current.estimated_prompt = estimated + tool_tokens if self._stats._current else 0
        self._stats._current.context_messages = len(msgs) if self._stats._current else 0

        check = self._budget.check_context(msgs, tools)
        if check["action"] == "hard_limit_exceeded":
            logger.warning("token budget hard limit exceeded: %s", check)
        elif check["action"] == "soft_limit_exceeded":
            logger.info("token budget soft limit exceeded: %s", check)

    async def _on_after_model_response(self, ctx: HookContext) -> None:
        usage = getattr(ctx.model_response, "usage_metadata", None) or {}
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cache_hit = int(usage.get("cache_read_input_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
        cache_miss = int(usage.get("cache_creation_input_tokens") or usage.get("prompt_cache_miss_tokens") or 0)
        cache_write = int(usage.get("prompt_cache_write_tokens") or 0)
        self._stats.record_usage(inp, out, cache_hit=cache_hit, cache_miss=cache_miss, cache_write=cache_write)

    async def _on_tool_calls_parsed(self, ctx: HookContext) -> None:
        for _ in ctx.tool_calls or []:
            self._stats.record_tool_call()

    async def _on_turn_end(self, ctx: HookContext) -> None:
        self._stats.finish_turn()

    async def _on_before_state_persist(self, ctx: HookContext) -> None:
        state = self._stats.summary()
        ctx.state["token_stats"] = state

    def summary(self) -> dict[str, Any]:
        return self._stats.summary()
