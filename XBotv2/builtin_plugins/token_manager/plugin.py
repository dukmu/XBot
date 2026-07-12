"""TokenManagerPlugin — token estimation, usage tracking, and budget control."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    HookContext,
    HookStage,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
)

from .budget import TokenBudgetController
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

    async def on_unload(self) -> None:
        self._stats.reset()

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_TURN_START, self._on_turn_start)
        ctx.register_hook(HookStage.BEFORE_MODEL_REQUEST, self._on_before_model_request)
        ctx.register_hook(HookStage.AFTER_MODEL_RESPONSE, self._on_after_model_response)
        ctx.register_hook(HookStage.ON_TOOL_CALLS_PARSED, self._on_tool_calls_parsed)
        ctx.register_hook(HookStage.ON_TURN_END, self._on_turn_end)

    async def _on_turn_start(self, ctx: HookContext) -> None:
        self._stats.start_turn(turn=ctx.session.turn_count)

    async def _on_before_model_request(self, ctx: HookContext) -> None:
        request = ctx.model_request or {}
        msgs = list(request.get("messages") or [])
        tools = list(request.get("tools") or [])
        check = self._budget.check_context(msgs, tools)
        self._stats.update_context(
            estimated_prompt=check["total_estimated"],
            message_count=len(msgs),
        )

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

    def summary(self) -> dict[str, Any]:
        return self._stats.summary()

    def diagnostics(self) -> dict[str, Any]:
        return {"status": "ready", "usage": self.summary()}
