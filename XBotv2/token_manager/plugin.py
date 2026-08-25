"""Observe the latest model request without owning context policy."""

from __future__ import annotations

from typing import Any

from XBotv2.core import (
    calibrated_context_tokens,
)
from XBotv2.agentloop import EventContext, Events


class TokenManagerPlugin:
    inject = ['session']
    name = "token_manager"

    def __init__(self) -> None:
        self._latest: dict[str, Any] = {}

    def apply(self, ctx, config=None) -> None:
        ctx.on(Events.BEFORE_MODEL_REQUEST, self._on_before_model_request)
        ctx.on(Events.AFTER_MODEL_RESPONSE, self._on_after_model_response)

    async def _on_before_model_request(self, ctx: EventContext) -> None:
        request = ctx.model_request
        messages = list(request.messages) if request is not None else []
        tools = list(request.tools) if request is not None else []
        if ctx.settings is None or ctx.session is None:
            raise RuntimeError("Token manager requires a complete request context")
        context_window = int(ctx.settings.context_window or 0)
        context_tokens, raw_estimate, source = calibrated_context_tokens(
            messages,
            tools,
            list(ctx.messages),
            provider=ctx.session.provider,
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

    async def _on_after_model_response(self, ctx: EventContext) -> None:
        if ctx.model_response is None:
            raise RuntimeError("Token manager requires a model response")
        usage = ctx.model_response.usage_metadata
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


plugin = TokenManagerPlugin()
