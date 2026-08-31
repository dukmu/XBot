"""Pure compaction proposal construction and auxiliary model call."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from XBotv2.core import ClientEvent, Message, estimate_messages_tokens

from XBotv2.compact.history import compact_prefix_end, history_chars
from XBotv2.compact.protocol import compact_event
from XBotv2.compact.summary import (
    compacted_message,
    invoke_llm,
    model_usage,
    normalize_summary,
    summary_request,
)

logger = logging.getLogger("xbotv2.compact")

RuntimePublisher = Callable[[ClientEvent], Awaitable[None]]
UsageRecorder = Callable[[dict[str, int]], Awaitable[None]]


async def build_compaction_proposal(
    *,
    model: Any,
    record_usage: UsageRecorder,
    publish_runtime_event: RuntimePublisher,
    session: Any,
    messages: list[Message],
    reason: str,
    keep_recent_turns: int,
    summary_max_chars: int,
    context_tokens_before: int,
    estimate_source: str,
    request_estimate: int | None = None,
    context_limit: int | None = None,
    max_context_tokens: int | None = None,
    output_reservation: int | None = None,
    stable_prefix: Sequence[Any] = (),
    removable_estimate: int | None = None,
) -> dict[str, Any] | None:
    split = compact_prefix_end(messages, keep_recent_turns)
    if split == 0:
        return None

    prefix_messages = messages[:split]
    removed_estimate = (
        int(removable_estimate)
        if removable_estimate is not None
        else estimate_messages_tokens(prefix_messages)
    )

    # If even an empty summary envelope cannot be smaller than the removable
    # prefix, an automatic auxiliary call cannot improve the request.
    if reason == "automatic":
        minimum_summary_estimate = estimate_messages_tokens([
            compacted_message("x", reason=reason)
        ])
        if removed_estimate <= minimum_summary_estimate:
            return None

    turn = int(session.turn_count or 0)
    chars_before = history_chars(messages)
    logger.info(
        "compaction started reason=%s turn=%d messages=%d history_chars=%d "
        "context_tokens=%d context_limit=%s estimate_source=%s",
        reason,
        turn,
        len(messages),
        chars_before,
        context_tokens_before,
        context_limit,
        estimate_source,
    )
    await publish_runtime_event(compact_event(
        "compaction_started",
        {
            "reason": reason,
            "messages_before": len(messages),
            "history_chars_before": chars_before,
            "context_tokens_before": context_tokens_before,
            "context_limit": context_limit,
        },
    ))

    try:
        summary_messages = prefix_messages
        if stable_prefix:
            # ContextBuilder folds retained system-history messages into the
            # stable provider system prefix. Avoid supplying those messages a
            # second time while still letting the previous compaction summary
            # participate through that stable prefix.
            summary_messages = [
                message for message in summary_messages
                if message.role != "system"
            ]
        response = await invoke_llm(
            model,
            summary_request(
                summary_messages,
                summary_max_chars,
                stable_prefix=stable_prefix,
            ),
        )
        await record_usage(model_usage(response.usage_metadata))
        if response.tool_calls:
            raise RuntimeError("Compaction model must not call tools")
        summary, summary_truncated = normalize_summary(
            str(response.content or ""),
            summary_max_chars,
        )
    except asyncio.CancelledError:
        await publish_runtime_event(compact_event(
            "compaction_failed",
            {"reason": reason, "message": "Compaction cancelled."},
        ))
        raise
    except Exception as exc:
        await publish_runtime_event(compact_event(
            "compaction_failed",
            {"reason": reason, "message": str(exc)},
        ))
        if reason == "manual":
            raise
        logger.exception(
            "automatic compaction failed; continuing with original history"
        )
        return None

    compacted = compacted_message(summary, reason=reason)
    compacted_messages = [compacted, *messages[split:]]
    usage = model_usage(response.usage_metadata)
    summary_estimate = estimate_messages_tokens([compacted])
    context_tokens_after = max(
        1,
        context_tokens_before - removed_estimate + summary_estimate,
    )
    if reason == "automatic" and context_tokens_after >= context_tokens_before:
        message = (
            "Automatic compaction would not reduce the estimated context; "
            "the generated summary is not smaller than the removable prefix."
        )
        logger.warning(message)
        await publish_runtime_event(compact_event(
            "compaction_failed",
            {"reason": reason, "message": message},
        ))
        return None

    metrics = {
        "context_tokens_before": context_tokens_before,
        "context_tokens_after_estimate": context_tokens_after,
        "context_tokens_released_estimate": max(
            0,
            context_tokens_before - context_tokens_after,
        ),
        "context_limit": context_limit,
        "max_context_tokens": max_context_tokens,
        "output_reservation": output_reservation,
        "request_estimate": request_estimate,
        "estimate_source": estimate_source,
        "history_chars_before": chars_before,
        "history_chars_after": history_chars(compacted_messages),
        "summary_chars": len(summary),
        "summary_truncated": summary_truncated,
        "messages_before": len(messages),
        "messages_after": len(compacted_messages),
        "messages_removed": len(messages) - len(compacted_messages),
        "model_usage": usage,
    }
    return {
        "messages": compacted_messages,
        "compact_reason": reason,
        "compact_metrics": metrics,
    }


__all__ = ["build_compaction_proposal"]
