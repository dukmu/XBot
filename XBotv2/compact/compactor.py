"""Pure compaction proposal construction and auxiliary model call."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4
from pydantic import JsonValue

from XBotv2.core import (
    ClientEvent,
    Message,
    ModelResponse,
    estimate_messages_tokens,
    estimate_request_tokens,
)
from XBotv2.core.tools import Tool
from XBotv2.core.timing import SESSION_STATS_METADATA_KEY, conversation_stats
from XBotv2.llm.contracts import ModelPort
from XBotv2.session.contracts import SessionInfo

from XBotv2.compact.history import (
    compact_prefix_end,
    history_chars,
    tool_pairing_boundaries,
)
from XBotv2.compact.contracts import CompactionProposal
from XBotv2.compact.protocol import (
    CompactionMetrics,
    CompactionReason,
    compact_event,
    is_automatic_compaction,
)
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
TrajectoryRecorder = Callable[[str, dict[str, JsonValue]], None]


def _response_trace(response: ModelResponse) -> dict[str, JsonValue]:
    """Return the complete provider-neutral response fields as JSON."""
    return {
        "content": response.content,
        "reasoning": response.reasoning,
        "response_metadata": response.response_metadata,
        "additional_kwargs": response.additional_kwargs,
    }


def _summary_input(
    messages: Sequence[Message],
    split: int,
    stable_prefix: Sequence[Message],
    summary_max_chars: int,
) -> list[Message]:
    prefix = list(messages[:split])
    if stable_prefix:
        prefix = [message for message in prefix if message.role != "system"]
    return summary_request(
        prefix,
        summary_max_chars,
        stable_prefix=stable_prefix,
    )


def _fit_summary_prefix(
    messages: list[Message],
    split: int,
    *,
    stable_prefix: Sequence[Message],
    tools: Sequence[Tool],
    summary_max_chars: int,
    max_context_tokens: int | None,
    summary_output_tokens: int,
) -> int:
    """Fit the complete auxiliary request envelope at a safe surface cut."""
    if max_context_tokens is None:
        return split
    reserve = max(1, int(summary_output_tokens))
    available = max_context_tokens - reserve
    if available < 1:
        raise RuntimeError(
            "Compaction auxiliary request has no context available after "
            "the configured output reservation"
        )
    # The envelope is a fixed prefix plus a monotonic contribution from each
    # selected message.  Fold it once and remember the largest safe cut that
    # fits; repeatedly estimating every candidate made large histories O(n²).
    tool_list = list(tools)
    base = estimate_request_tokens(
        _summary_input(messages, 0, stable_prefix, summary_max_chars),
        tool_list,
    )
    boundaries = tool_pairing_boundaries(messages)
    estimate = base
    best = 0 if estimate <= available else None
    for index, message in enumerate(messages[:split], start=1):
        if not stable_prefix or message.role != "system":
            estimate += estimate_messages_tokens([message])
        if estimate > available:
            break
        if boundaries[index]:
            best = index
    if best is not None:
        return best
    raise RuntimeError(
        "Compaction auxiliary request exceeds the model context window even "
        "with an empty removable history prefix"
    )


async def build_compaction_proposal(
    *,
    model: ModelPort,
    record_usage: UsageRecorder,
    publish_runtime_event: RuntimePublisher,
    session: SessionInfo,
    messages: list[Message],
    reason: CompactionReason,
    keep_recent_turns: int,
    summary_max_chars: int,
    context_tokens_before: int,
    estimate_source: str,
    request_estimate: int | None = None,
    context_limit: int | None = None,
    max_context_tokens: int | None = None,
    output_reservation: int | None = None,
    summary_output_tokens: int = 2_048,
    stable_prefix: Sequence[Message] = (),
    tools: Sequence[Tool] = (),
    removable_estimate: int | None = None,
    record_trajectory: TrajectoryRecorder | None = None,
) -> CompactionProposal | None:
    # An overflow recovery must keep only the newest turn; that also relaxes the
    # "must shrink" guards below, so the reason alone drives both decisions.
    split = compact_prefix_end(
        messages,
        1 if reason == "context-overflow" else keep_recent_turns,
    )
    try:
        split = _fit_summary_prefix(
            messages,
            split,
            stable_prefix=stable_prefix,
            tools=tools,
            summary_max_chars=summary_max_chars,
            max_context_tokens=max_context_tokens,
            summary_output_tokens=summary_output_tokens,
        )
    except RuntimeError:
        if reason == "manual":
            raise
        logger.warning("automatic compaction auxiliary request cannot fit context")
        return None
    if split == 0:
        return None

    compaction_id = uuid4().hex

    prefix_messages = messages[:split]
    removed_estimate = (
        int(removable_estimate)
        if removable_estimate is not None
        else estimate_messages_tokens(prefix_messages)
    )

    # Threshold compaction may be pointless; manual and context-overflow
    # compaction must still run even when the estimate says the summary will
    # not shrink the request.
    threshold_triggered = reason == "automatic"
    if threshold_triggered:
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
    if record_trajectory is not None:
        record_trajectory("compaction/start", {
            "compaction_id": compaction_id,
            "reason": reason,
            "messages_before": len(messages),
            "prefix_messages": split,
            "context_tokens_before": context_tokens_before,
        })

    try:
        response = await invoke_llm(
            model,
            _summary_input(messages, split, stable_prefix, summary_max_chars),
            output_tokens=summary_output_tokens,
        )
        await record_usage(model_usage(response.usage_metadata))
        if response.tool_calls:
            raise RuntimeError("Compaction model must not call tools")
        summary, summary_truncated = normalize_summary(
            str(response.content or ""),
            summary_max_chars,
        )
    except asyncio.CancelledError:
        if record_trajectory is not None:
            record_trajectory("compaction/end", {
                "compaction_id": compaction_id,
                "error": "cancelled",
            })
        await publish_runtime_event(compact_event(
            "compaction_failed",
            {
                "reason": reason,
                "message": "Compaction cancelled.",
                "automatic": is_automatic_compaction(reason),
            },
        ))
        raise
    except Exception as exc:
        if record_trajectory is not None:
            record_trajectory("compaction/end", {
                "compaction_id": compaction_id,
                "error": str(exc),
            })
        await publish_runtime_event(compact_event(
            "compaction_failed",
            {
                "reason": reason,
                "message": str(exc),
                "automatic": is_automatic_compaction(reason),
            },
        ))
        if reason == "manual":
            raise
        logger.exception(
            "automatic compaction failed; continuing with original history"
        )
        return None

    compacted = compacted_message(summary, reason=reason)
    compacted.response_metadata[SESSION_STATS_METADATA_KEY] = (
        conversation_stats(prefix_messages).model_dump(mode="json")
    )
    compacted_messages = [compacted, *messages[split:]]
    usage = model_usage(response.usage_metadata)
    summary_estimate = estimate_messages_tokens([compacted])
    context_tokens_after = max(
        1,
        context_tokens_before - removed_estimate + summary_estimate,
    )
    if threshold_triggered and context_tokens_after >= context_tokens_before:
        message = (
            "Automatic compaction would not reduce the estimated context; "
            "the generated summary is not smaller than the removable prefix."
        )
        logger.warning(message)
        await publish_runtime_event(compact_event(
            "compaction_failed",
            {
                "reason": reason,
                "message": message,
                "automatic": is_automatic_compaction(reason),
            },
        ))
        if record_trajectory is not None:
            record_trajectory("compaction/end", {
                "compaction_id": compaction_id,
                "error": message,
            })
        return None

    metrics = CompactionMetrics(
        context_tokens_before=context_tokens_before,
        context_tokens_after_estimate=context_tokens_after,
        context_tokens_released_estimate=max(
            0,
            context_tokens_before - context_tokens_after,
        ),
        context_limit=context_limit,
        max_context_tokens=max_context_tokens,
        output_reservation=output_reservation,
        summary_output_tokens=summary_output_tokens,
        request_estimate=request_estimate,
        estimate_source=estimate_source,
        history_chars_before=chars_before,
        history_chars_after=history_chars(compacted_messages),
        summary_chars=len(summary),
        summary_truncated=summary_truncated,
        messages_before=len(messages),
        messages_after=len(compacted_messages),
        messages_removed=len(messages) - len(compacted_messages),
        model_usage=usage,
    )
    return {
        "messages": compacted_messages,
        "prefix_end": split,
        "compaction_id": compaction_id,
        "summary": summary,
        "raw_output": _response_trace(response),
        "compact_reason": reason,
        "compact_metrics": metrics,
    }


__all__ = ["build_compaction_proposal"]
