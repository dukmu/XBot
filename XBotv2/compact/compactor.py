"""Pure compaction proposal construction and auxiliary model call."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4

from XBotv2.core import estimate_messages_tokens, estimate_model_request_tokens
from XBotv2.core.messages import ConversationMessage
from XBotv2.core.domain import (
    AuxiliaryRequest,
    RequestObservation,
    ResolvedModelSelection,
    TransactionAborted,
    TransactionFailed,
    TransactionEnded,
    TransactionRef,
    TransactionStarted,
    UsageDelta,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ModelRequest
from XBotv2.core.tools import ToolCall
from XBotv2.core.timing import conversation_stats
from XBotv2.llm.contracts import ModelPort
from XBotv2.session.contracts import SessionRuntimeState

from XBotv2.compact.history import (
    compact_prefix_end,
    history_chars,
    tool_pairing_boundaries,
)
from XBotv2.compact.contracts import CompactionPlan, CompactionSelection
from XBotv2.core.domain import HistoryRevision, MessageId
from XBotv2.compact.protocol import (
    CompactionFailed,
    CompactionMetrics,
    CompactionReason,
    CompactionStarted,
    is_automatic_compaction,
)
from XBotv2.compact.summary import (
    compacted_message,
    normalize_summary,
    summary_request,
)
from XBotv2.llm import invoke_llm

logger = logging.getLogger("xbotv2.compact")

RuntimePublisher = Callable[[object], Awaitable[None]]
UsageRecorder = Callable[[RequestObservation, UsageDelta], Awaitable[None]]
TrajectoryRecorder = Callable[[TransactionStarted | TransactionEnded], None]


def _summary_input(
    messages: Sequence[ConversationMessage],
    split: int,
    stable_prefix: Sequence[ConversationMessage],
    summary_max_chars: int,
) -> list:
    prefix = list(messages[:split])
    if stable_prefix:
        prefix = list(prefix)
    return summary_request(
        prefix,
        summary_max_chars,
        stable_prefix=stable_prefix,
    )


def _fit_summary_prefix(
    messages: list[ConversationMessage],
    split: int,
    *,
    stable_prefix: Sequence[ConversationMessage],
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
    base = estimate_messages_tokens(
        _summary_input(messages, 0, stable_prefix, summary_max_chars),
    )
    boundaries = tool_pairing_boundaries(messages)
    estimate = base
    best = 0 if estimate <= available else None
    for index, message in enumerate(messages[:split], start=1):
        if not stable_prefix:
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


async def build_compaction_plan(
    *,
    model: ModelPort,
    selection: ResolvedModelSelection,
    record_usage: UsageRecorder,
    publish_runtime_event: RuntimePublisher,
    session: SessionRuntimeState,
    messages: list[ConversationMessage],
    history_revision: HistoryRevision,
    source_ids: tuple[MessageId, ...],
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
    stable_prefix: Sequence[ConversationMessage] = (),
    removable_estimate: int | None = None,
    record_trajectory: TrajectoryRecorder | None = None,
) -> CompactionPlan | None:
    if len(source_ids) != len(messages):
        raise ValueError("Compaction source identities must match the input history")
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
    transaction = TransactionRef(kind="compaction", id=compaction_id)

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
    await publish_runtime_event(CompactionStarted(
        reason=reason,
        messages_before=len(messages),
        history_chars_before=chars_before,
        context_tokens_before=context_tokens_before,
        context_limit=context_limit,
    ))
    if record_trajectory is not None:
        record_trajectory(TransactionStarted(transaction=transaction))

    try:
        request_messages = tuple(
            _summary_input(messages, split, stable_prefix, summary_max_chars)
        )
        auxiliary_selection = selection.model_copy(update={
            "generation": selection.generation.model_copy(update={
                "max_output_tokens": summary_output_tokens,
            }),
        })
        model_request = ModelRequest(
            messages=request_messages,
            tools=(),
            selection=auxiliary_selection,
        )
        response = await invoke_llm(model, model_request)
        await record_usage(
            RequestObservation(
                selection=auxiliary_selection,
                purpose=AuxiliaryRequest(
                    owner="compact",
                    operation_id=compaction_id,
                ),
                estimated_input_tokens=estimate_model_request_tokens(model_request),
                observed_context=response.observed_context,
            ),
            response.usage,
        )
        if any(isinstance(part, ToolCall) for part in response.parts):
            raise RuntimeError("Compaction model must not call tools")
        summary, summary_truncated = normalize_summary(
            "".join(part.text for part in response.parts if isinstance(part, TextPart)),
            summary_max_chars,
        )
    except asyncio.CancelledError:
        if record_trajectory is not None:
            record_trajectory(TransactionEnded(
                transaction=transaction,
                outcome=TransactionAborted(reason="cancelled"),
            ))
        await publish_runtime_event(CompactionFailed(
            reason=reason,
            message="Compaction cancelled.",
            automatic=is_automatic_compaction(reason),
        ))
        raise
    except Exception as exc:
        if record_trajectory is not None:
            record_trajectory(TransactionEnded(
                transaction=transaction,
                outcome=TransactionFailed(error=str(exc) or type(exc).__name__),
            ))
        await publish_runtime_event(CompactionFailed(
            reason=reason,
            message=str(exc) or type(exc).__name__,
            automatic=is_automatic_compaction(reason),
        ))
        if reason == "manual":
            raise
        logger.exception(
            "automatic compaction failed; continuing with original history"
        )
        return None

    compacted = compacted_message(summary, reason=reason)
    compacted_messages = [compacted, *messages[split:]]
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
        await publish_runtime_event(CompactionFailed(
            reason=reason,
            message=message,
            automatic=is_automatic_compaction(reason),
        ))
        if record_trajectory is not None:
            record_trajectory(TransactionEnded(
                transaction=transaction,
                outcome=TransactionAborted(reason=message),
            ))
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
        model_usage=response.usage.counters,
    )
    return CompactionPlan(
        id=compaction_id,
        reason=reason,
        selection=CompactionSelection(
            expected_revision=history_revision,
            source_ids=source_ids[:split],
        ),
        summary=compacted,
        metrics=metrics,
    )


__all__ = ["build_compaction_plan"]
