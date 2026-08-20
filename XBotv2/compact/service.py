"""Compact plugin runtime service and history-commit ownership."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from XBotv2.core import (
    ClientEvent,
    Message,
    calibrated_context_tokens,
    context_token_limit,
    estimate_messages_tokens,
)
from XBotv2.agentloop import EventContext, Events, LoopSettings
from XBotv2.commands import CommandResult
from XBotv2.session import HISTORY_CHANGED, HistoryChanged

from XBotv2.compact.commands import run_compact_command
from XBotv2.compact.compactor import build_compaction_proposal
from XBotv2.compact.config import CompactConfig
from XBotv2.compact.history import history_chars, leading_system_messages
from XBotv2.compact.protocol import compact_event

logger = logging.getLogger("xbotv2.compact")


class CompactService:
    """Own compaction runtime state, proposal generation, and commit semantics."""

    def __init__(self) -> None:
        self._automatic = True
        self._output_reservation: int | None = None
        self._trigger_ratio = 0.8
        self._keep_recent_turns = 4
        self._summary_max_chars = 8_000
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""
        self._last_compaction: dict[str, Any] = {}

    def bind(self, ctx: Any, model: Any, config: CompactConfig) -> None:
        self.ctx = ctx
        self.model = model
        self.state = ctx.loop_state
        self._automatic = config.automatic
        self._output_reservation = config.output_reservation
        self._trigger_ratio = config.trigger_ratio
        self._keep_recent_turns = config.keep_recent_turns
        self._summary_max_chars = config.summary_max_chars

    async def _on_unload(self) -> None:
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""
        self._last_compaction = {}

    def request_manual_compaction(self) -> None:
        self._manual_requested = True

    def _consume_manual_request(self, session: Any = None) -> bool:
        del session
        if not self._manual_requested:
            return False
        self._manual_requested = False
        return True

    async def _compact_command(
        self,
        raw_args: str,
    ) -> CommandResult:
        return await run_compact_command(self, raw_args)

    async def _compact_current_history(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        event_ctx = EventContext(
            messages=self.state.messages,
            session=self.state.session,
        )
        messages = list(event_ctx.messages)
        proposal = await self._compact(
            event_ctx,
            messages,
            reason="manual",
            context_tokens_before=estimate_messages_tokens(messages),
            estimate_source="estimated_history",
        )
        result = await self._commit(event_ctx, proposal)
        metrics = (
            dict(proposal.get("compact_metrics") or {})
            if proposal is not None and result and result.get("rebuild")
            else {}
        )
        return result, metrics

    async def _on_before_context(self, ctx: EventContext):
        if not self._consume_manual_request(ctx.session):
            return None
        messages = list(ctx.messages)
        proposal = await self._compact(
            ctx,
            messages,
            reason="manual",
            context_tokens_before=estimate_messages_tokens(messages),
            estimate_source="estimated_history",
        )
        return await self._commit(ctx, proposal)

    async def _on_before_model_request(self, ctx: EventContext):
        if not self._automatic:
            return None

        messages = list(ctx.messages)
        request = ctx.model_request or {}
        context_messages = list(request.get("messages") or [])
        tools = list(request.get("tools") or [])
        max_context = _context_window(ctx.settings)
        context_tokens, request_estimate, estimate_source = calibrated_context_tokens(
            context_messages,
            tools,
            messages,
            provider=str(getattr(ctx.session, "provider", "") or ""),
            context_window=max_context,
        )
        configured_output = max(
            0,
            int(getattr(ctx.settings, "max_output_tokens", 0) or 0),
        )
        output_reservation = (
            self._output_reservation
            if self._output_reservation is not None
            else configured_output
        )
        token_trigger = context_token_limit(
            max_context,
            trigger_ratio=self._trigger_ratio,
            output_reservation=output_reservation,
        )
        if context_tokens < token_trigger:
            return None

        proposal = await self._compact(
            ctx,
            messages,
            reason="automatic",
            context_tokens_before=context_tokens,
            estimate_source=estimate_source,
            request_estimate=request_estimate,
            context_limit=token_trigger,
            max_context_tokens=max_context,
            output_reservation=output_reservation,
            stable_prefix=leading_system_messages(context_messages),
        )
        return await self._commit(ctx, proposal)

    async def _commit(
        self,
        ctx: EventContext,
        proposal: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Commit one already-built proposal at the history ownership boundary."""
        if proposal is None:
            return None

        original_messages = list(ctx.messages)
        reason = str(proposal.get("compact_reason") or "context")
        proposed_messages = list(proposal["messages"])
        pre = EventContext(
            messages=list(proposed_messages),
            session=ctx.session,
            event={"reason": reason},
        )
        pre_result = await self.ctx.serial(Events.PRE_COMPACT, pre)

        # PRE_COMPACT supports both EventContext mutation and explicit dict
        # replacement.  This matches the rest of XBot's event contract.
        messages = list(pre.messages)
        if isinstance(pre.event, dict):
            reason = str(pre.event.get("reason") or reason)
        if isinstance(pre_result, dict):
            messages = list(pre_result.get("messages", messages))
            reason = str(pre_result.get("compact_reason") or reason)
        elif pre_result is not None:
            message = "Compaction was rejected before commit."
            await self._publish_runtime_event(compact_event(
                "compaction_failed",
                {"reason": reason, "message": message},
            ))
            return {
                "event": {
                    "type": "error",
                    "data": {
                        "code": "hook_rejected",
                        "message": message,
                        "stage": Events.PRE_COMPACT,
                    },
                },
                "turn_complete": True,
            }

        metrics = _finalize_metrics(
            proposal.get("compact_metrics") or {},
            original_messages=original_messages,
            proposed_messages=proposed_messages,
            committed_messages=messages,
        )
        proposal["compact_reason"] = reason
        proposal["messages"] = messages
        proposal["compact_metrics"] = metrics

        previous_count = len(original_messages)
        ctx.messages[:] = messages
        committed = EventContext(
            messages=ctx.messages,
            session=ctx.session,
            event={
                "reason": reason,
                "metrics": metrics,
                "previous_message_count": previous_count,
                "current_message_count": len(ctx.messages),
            },
        )
        await self.ctx.emit(Events.POST_COMPACT, committed)
        await self.ctx.emit(
            HISTORY_CHANGED,
            HistoryChanged(
                tuple(ctx.messages),
                operation=f"compact:{reason}",
            ),
        )

        self._record_committed(reason, metrics, ctx.session)
        await self._publish_runtime_event(compact_event(
            "compaction_completed",
            {"reason": reason, "metrics": metrics},
        ))
        return {"rebuild": True}

    async def _compact(
        self,
        ctx: EventContext,
        messages: list[Message],
        *,
        reason: str,
        context_tokens_before: int,
        estimate_source: str,
        request_estimate: int | None = None,
        context_limit: int | None = None,
        max_context_tokens: int | None = None,
        output_reservation: int | None = None,
        stable_prefix: Message | Sequence[Any] | None = None,
        removable_estimate: int | None = None,
    ) -> dict[str, Any] | None:
        if stable_prefix is None:
            stable: Sequence[Any] = ()
        elif isinstance(stable_prefix, Message):
            stable = (stable_prefix,)
        else:
            stable = tuple(stable_prefix)
        return await build_compaction_proposal(
            model=self.model,
            publish_runtime_event=self._publish_runtime_event,
            session=ctx.session,
            messages=messages,
            reason=reason,
            keep_recent_turns=self._keep_recent_turns,
            summary_max_chars=self._summary_max_chars,
            context_tokens_before=context_tokens_before,
            estimate_source=estimate_source,
            request_estimate=request_estimate,
            context_limit=context_limit,
            max_context_tokens=max_context_tokens,
            output_reservation=output_reservation,
            stable_prefix=stable,
            removable_estimate=removable_estimate,
        )

    async def _publish_runtime_event(self, event: ClientEvent) -> None:
        await self.ctx.emit(
            Events.RUNTIME_EVENT,
            EventContext(client_event=event),
        )

    def _record_committed(
        self,
        reason: str,
        metrics: dict[str, Any],
        session: Any,
    ) -> None:
        self._compactions += 1
        self._last_reason = reason
        self._last_compaction = dict(metrics)
        usage = metrics.get("model_usage") or {}
        logger.info(
            "compaction completed reason=%s turn=%d messages_before=%d "
            "messages_after=%d history_chars_before=%d history_chars_after=%d "
            "context_tokens_before=%d context_tokens_after_estimate=%d "
            "summary_chars=%d input_tokens=%d output_tokens=%d total_tokens=%d",
            reason,
            int(getattr(session, "turn_count", 0) or 0),
            metrics.get("messages_before", 0),
            metrics.get("messages_after", 0),
            metrics.get("history_chars_before", 0),
            metrics.get("history_chars_after", 0),
            metrics.get("context_tokens_before", 0),
            metrics.get("context_tokens_after_estimate", 0),
            metrics.get("summary_chars", 0),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("total_tokens", 0),
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "automatic": self._automatic,
            "output_reservation": self._output_reservation,
            "trigger_ratio": self._trigger_ratio,
            "keep_recent_turns": self._keep_recent_turns,
            "compactions": self._compactions,
            "last_reason": self._last_reason,
            "last_compaction": dict(self._last_compaction),
        }


def _context_window(settings: LoopSettings | None) -> int:
    value = settings.context_window if settings is not None else 32_000
    result = int(value)
    return result if result > 0 else 32_000


def _finalize_metrics(
    metrics: dict[str, Any],
    *,
    original_messages: list[Message],
    proposed_messages: list[Message],
    committed_messages: list[Message],
) -> dict[str, Any]:
    finalized = dict(metrics)
    finalized["history_chars_before"] = history_chars(original_messages)
    finalized["history_chars_after"] = history_chars(committed_messages)
    finalized["messages_before"] = len(original_messages)
    finalized["messages_after"] = len(committed_messages)
    finalized["messages_removed"] = len(original_messages) - len(committed_messages)

    predicted_after = int(finalized.get("context_tokens_after_estimate") or 0)
    if predicted_after > 0 and committed_messages != proposed_messages:
        delta = (
            estimate_messages_tokens(committed_messages)
            - estimate_messages_tokens(proposed_messages)
        )
        predicted_after = max(1, predicted_after + delta)
        finalized["context_tokens_after_estimate"] = predicted_after
        before = int(finalized.get("context_tokens_before") or 0)
        finalized["context_tokens_released_estimate"] = max(
            0,
            before - predicted_after,
        )
    return finalized


__all__ = ["CompactService"]
