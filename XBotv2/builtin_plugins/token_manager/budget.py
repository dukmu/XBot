"""Token budget controller — enforces max_context_tokens, coordinates with compaction."""

from __future__ import annotations

import logging
from typing import Any

from .estimator import estimate_context_tokens, estimate_tool_schema_tokens

logger = logging.getLogger("xbotv2.token_manager")


class TokenBudgetController:
    def __init__(
        self,
        max_context_tokens: int = 32000,
        output_reservation: int = 4096,
        soft_limit_ratio: float = 0.8,
    ) -> None:
        self.max_context = max_context_tokens
        self.output_reservation = output_reservation
        self.soft_limit_ratio = soft_limit_ratio
        self.hard_limit = max_context_tokens - output_reservation
        self.soft_limit = int(self.hard_limit * soft_limit_ratio)

    def check_context(
        self, context_messages: list[Any], tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Check if context is within budget. Returns status dict."""
        estimated = estimate_context_tokens(context_messages)
        tool_tokens = estimate_tool_schema_tokens(tools) if tools else 0
        total = estimated + tool_tokens

        status = {
            "estimated_tokens": estimated,
            "tool_schema_tokens": tool_tokens,
            "total_estimated": total,
            "hard_limit": self.hard_limit,
            "soft_limit": self.soft_limit,
        }

        if total > self.hard_limit:
            status["action"] = "hard_limit_exceeded"
            status["reason"] = f"Context {total} tokens exceeds hard limit {self.hard_limit}"
        elif total > self.soft_limit:
            status["action"] = "soft_limit_exceeded"
            status["reason"] = f"Context {total} tokens exceeds soft limit {self.soft_limit}"
        else:
            status["action"] = "ok"

        return status
