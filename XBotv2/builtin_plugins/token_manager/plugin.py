"""Observe the latest model request without owning context policy."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    HookContext,
    HookStage,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    calibrated_context_tokens,
)


class TokenManagerPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._latest: dict[str, Any] = {}

    async def on_load(self, config: dict[str, Any]) -> None:
        del config

    async def on_unload(self) -> None:
        self._latest = {}

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.BEFORE_MODEL_REQUEST, self._on_before_model_request)
        ctx.register_hook(HookStage.AFTER_MODEL_RESPONSE, self._on_after_model_response)

    async def _on_before_model_request(self, ctx: HookContext) -> None:
        request = ctx.model_request or {}
        messages = list(request.get("messages") or [])
        tools = list(request.get("tools") or [])
        context_window = int(
            getattr(ctx.config, "max_context_tokens", 0) or 0
        )
        context_tokens, raw_estimate, source = calibrated_context_tokens(
            messages,
            tools,
            list(ctx.messages),
            provider=str(getattr(ctx.session, "provider", "") or ""),
            context_window=context_window,
        )
        self._latest = {
            "turn": ctx.session.turn_count,
            "message_count": len(messages),
            "tool_count": len(tools),
            "context_window": context_window,
            "context_tokens_estimate": context_tokens,
            "raw_estimate": raw_estimate,
            "estimate_source": source,
            "utilization": (
                context_tokens / context_window if context_window > 0 else None
            ),
        }

    async def _on_after_model_response(self, ctx: HookContext) -> None:
        usage = getattr(ctx.model_response, "usage_metadata", None) or {}
        self._latest["provider_usage"] = {
            key: int(usage[key])
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "context_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "prompt_cache_write_tokens",
            )
            if usage.get(key) is not None
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "mode": "observe_only",
            "latest_request": dict(self._latest),
        }
