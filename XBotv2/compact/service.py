"""Compact plugin runtime service and history-commit ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol, Sequence
from pydantic import JsonValue

from XBotv2.application import RUNTIME_EVENT, RuntimeEvent
from XBotv2.core import (
    ClientEvent,
    Message,
    ProviderContextOverflowError,
    RequestAnchor,
    Tool,
    calibrated_context_tokens,
    context_token_limit,
    estimate_messages_tokens,
    estimate_request_tokens,
    write_request_anchor,
)
from XBotv2.agentloop import EventContext, Events, LoopSettings, LoopState
from XBotv2.commands import CommandResult
from XBotv2.llm.contracts import ModelPort
from XBotv2.session.contracts import HISTORY_CHANGED, HistoryChanged, SessionInfo

from XBotv2.compact.commands import run_compact_command
from XBotv2.compact.compactor import build_compaction_proposal
from XBotv2.compact.contracts import CompactConfig
from XBotv2.compact.contracts import CompactionProposal
from XBotv2.compact.protocol import (
    COMPACTION_TRANSACTION,
    CompactionMetrics,
    CompactionReason,
    is_automatic_compaction,
)
from XBotv2.compact.events import (
    POST_COMPACT,
    PRE_COMPACT,
    AfterCompact,
    BeforeCompact,
)
from XBotv2.compact.history import history_chars, leading_system_messages
from XBotv2.compact.protocol import compact_event

logger = logging.getLogger("xbotv2.compact")


class CompactEventsPort(Protocol):
    async def serial(self, event: str, *args: object) -> object: ...

    async def emit(self, event: str, *args: object) -> None: ...


class UsagePort(Protocol):
    async def add(
        self,
        usage: dict[str, JsonValue],
        *,
        update_context: bool = True,
    ) -> dict[str, int] | None: ...

    async def update_context(self, context_tokens: int) -> dict[str, int]: ...


class CompactService:
    """Own compaction runtime state, proposal generation, and commit semantics."""

    def __init__(
        self,
        *,
        events: CompactEventsPort,
        model: ModelPort,
        state: LoopState,
        usage: UsagePort,
        config: CompactConfig,
    ) -> None:
        self._events = events
        self.model = model
        self.state = state
        self._usage = usage
        self._automatic = config.automatic
        self._output_reservation = config.output_reservation
        self._trigger_ratio = config.trigger_ratio
        self._keep_recent_turns = config.keep_recent_turns
        self._summary_max_chars = config.summary_max_chars
        self._summary_output_tokens = config.summary_output_tokens
        self._manual_requested = False
        self._overflow_retries = 0
        self._overflow_owner: tuple[str, str] | None = None
        self._compactions = 0
        self._last_reason = ""
        self._last_compaction: CompactionMetrics | None = None

    async def _dispose(self) -> None:
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""
        self._last_compaction = None
        self._overflow_retries = 0
        self._overflow_owner = None

    def request_manual_compaction(self) -> None:
        self._manual_requested = True

    def _consume_manual_request(self) -> bool:
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
    ) -> tuple[dict[str, JsonValue] | None, CompactionMetrics | None]:
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
            proposal.get("compact_metrics")
            if proposal is not None and result and result.get("rebuild")
            else None
        )
        return result, metrics

    async def _on_before_context(self, ctx: EventContext):
        if not self._consume_manual_request():
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
        request = ctx.model_request
        if request is None or ctx.settings is None or ctx.session is None:
            raise RuntimeError("Compaction requires a complete model request context")
        context_messages = list(request.messages)
        tools = list(request.tools)
        max_context = _context_window(ctx.settings)
        context_tokens, request_estimate, estimate_source = calibrated_context_tokens(
            context_messages,
            tools,
            messages,
            provider=ctx.session.provider,
            model=ctx.settings.model,
            context_window=max_context,
        )
        configured_output = max(
            0,
            int(ctx.settings.max_output_tokens or 0),
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
            summary_output_tokens=self._summary_output_tokens,
            stable_prefix=leading_system_messages(context_messages),
            tools=tools,
        )
        return await self._commit(ctx, proposal)

    async def _on_model_request_error(self, ctx: EventContext):
        """Recover one provider-confirmed context overflow from durable history."""
        if ctx.session is not None:
            owner = (ctx.session.session_id, ctx.session.thread_id)
            if owner != self._overflow_owner:
                self._overflow_owner = owner
                self._overflow_retries = 0
        if (
            not self._automatic
            or ctx.error is None
            or not isinstance(ctx.error, ProviderContextOverflowError)
            or ctx.model_request is None
            or ctx.session is None
            or self._overflow_retries >= 1
        ):
            return None
        messages = list(ctx.messages)
        request = ctx.model_request
        source_nodes = self.state.history.node_ids()
        max_context = _context_window(ctx.settings)
        context_tokens, request_estimate, estimate_source = calibrated_context_tokens(
            list(request.messages), list(request.tools), messages,
            provider=ctx.session.provider,
            model=ctx.settings.model if ctx.settings is not None else "",
            context_window=max_context,
        )
        # The request's bound model is the active route/model at this exact
        # step.  The plugin's startup injection may point at an old provider.
        try:
            proposal = await self._compact(
                ctx,
                messages,
                reason="context-overflow",
                context_tokens_before=context_tokens,
                estimate_source=estimate_source,
                request_estimate=request_estimate,
                context_limit=max_context,
                max_context_tokens=max_context,
                output_reservation=int(ctx.settings.max_output_tokens or 0)
                if ctx.settings is not None else None,
                summary_output_tokens=self._summary_output_tokens,
                stable_prefix=leading_system_messages(request.messages),
                tools=list(request.tools),
            )
            if proposal is None:
                return None
            result = await self._commit(ctx, proposal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "context-overflow compaction failed; preserving provider error"
            )
            return None
        if (
            result
            and result.get("rebuild")
            and self.state.history.node_ids() != source_nodes
        ):
            self._overflow_retries += 1
            return {"retry": True}
        return None

    async def _on_after_model_response(self, _ctx: EventContext) -> None:
        self._overflow_retries = 0

    async def _on_turn_end(self, _ctx: EventContext) -> None:
        # A completed turn is the idle boundary for overflow recovery budget.
        self._overflow_retries = 0

    async def _commit(
        self,
        ctx: EventContext,
        proposal: CompactionProposal | None,
    ) -> dict[str, JsonValue] | None:
        """Commit one already-built proposal at the history ownership boundary."""
        if proposal is None:
            return None

        original_messages = list(ctx.messages)
        reason = proposal["compact_reason"]
        proposed_messages = list(proposal["messages"])
        pre = BeforeCompact(
            messages=list(proposed_messages),
            session=ctx.session,
            reason=reason,
        )
        try:
            pre_result = await self._events.serial(PRE_COMPACT, pre)
        except BaseException as exc:
            self._record_end(proposal, error=_exception_text(exc))
            raise

        messages = list(pre.messages)
        reason = pre.reason
        if pre_result is not None:
            message = "Compaction was rejected before commit."
            self._record_end(proposal, error=message)
            await self._publish_runtime_event(compact_event(
                "compaction_failed",
                {
                    "reason": reason,
                    "message": message,
                    "automatic": is_automatic_compaction(reason),
                },
            ))
            return {
                "event": {
                    "type": "error",
                    "data": {
                        "code": "hook_rejected",
                        "message": message,
                        "stage": PRE_COMPACT,
                    },
                },
                "turn_complete": True,
            }

        built = proposal.get("compact_metrics")
        if built is None:
            message = "Compaction proposal carries no metrics."
            self._record_end(proposal, error=message)
            raise RuntimeError(message)
        metrics = _finalize_metrics(
            built,
            original_messages=original_messages,
            proposed_messages=proposed_messages,
            committed_messages=messages,
        )
        proposal["compact_reason"] = reason
        proposal["messages"] = messages
        proposal["compact_metrics"] = metrics

        prefix_end = int(proposal["prefix_end"])
        retained = original_messages[prefix_end:]
        if retained and (
            len(messages) < len(retained)
            or messages[-len(retained):] != retained
        ):
            message = "PRE_COMPACT may only change the summary replacement."
            self._record_end(proposal, error=message)
            raise RuntimeError(message)
        replacement = messages[:len(messages) - len(retained)] if retained else messages
        if not replacement:
            message = "Compaction replacement must contain its summary message."
            self._record_end(proposal, error=message)
            raise RuntimeError(message)
        previous_count = len(original_messages)
        compaction_id = str(proposal["compaction_id"])
        try:
            request = ctx.model_request
            stable = (
                leading_system_messages(request.messages)
                if request is not None
                else []
            )
            projection = replacement[0]
            # A manual compaction has no request route to record, so its
            # projection is not anchored for later calibration.
            if request is not None:
                write_request_anchor(projection, RequestAnchor(
                    provider=ctx.session.provider if ctx.session is not None else "",
                    model=ctx.settings.model if ctx.settings is not None else "",
                    context_window=(
                        ctx.settings.context_window if ctx.settings is not None else 0
                    ),
                    request_estimate=estimate_request_tokens(
                        [*stable, *messages],
                        list(request.tools),
                    ),
                    context_tokens=metrics.context_tokens_after_estimate,
                ))
            # The replacement message in the surface replacement below is the
            # authoritative copy of the summary text; this marker carries only
            # metadata so one compaction never stores the same summary twice.
            self.state.history.record("compaction/summary", durable=True, data={
                "compaction_id": compaction_id,
                "reason": reason,
                "source_node_ids": list(proposal["source_node_ids"]),
                "provider": self._provider(ctx),
                "model": self._model(ctx),
                "usage": dict(metrics.model_usage),
                "metrics": metrics.model_dump(mode="json"),
            })
            self.state.replace_message_range(
                0,
                prefix_end,
                list(replacement),
                operation=f"compact:{compaction_id}",
                preserve_transcript=True,
            )
            ctx.messages = list(self.state.messages)
            usage_event = await self._usage.update_context(
                metrics.context_tokens_after_estimate
            )
            await self._publish_runtime_event(ClientEvent(type="usage", data=usage_event))
            committed = AfterCompact(
                messages=tuple(ctx.messages),
                session=ctx.session,
                reason=reason,
                metrics=metrics,
                previous_message_count=previous_count,
                current_message_count=len(ctx.messages),
            )
            await self._events.emit(POST_COMPACT, committed)
            await self._events.emit(
                HISTORY_CHANGED,
                HistoryChanged(
                    tuple(ctx.messages),
                    operation=f"compact:{reason}",
                ),
            )
        except BaseException as exc:
            self._record_end(proposal, error=_exception_text(exc))
            raise
        self._record_end(proposal)

        self._record_committed(reason, metrics, ctx.session)
        await self._publish_runtime_event(compact_event(
            "compaction_completed",
            {
                "reason": reason,
                "metrics": metrics,
                "automatic": is_automatic_compaction(reason),
                # The live summary is event-only: the durable trajectory keeps
                # the single copy inside the surface replacement.
                "summary": "\n".join(message.content for message in replacement),
            },
        ))
        return {"rebuild": True}

    def _provider(self, ctx: EventContext) -> str:
        metadata = self.state.metadata.value
        return str(
            metadata.provider
            or (ctx.settings.provider if ctx.settings is not None else "")
            or (ctx.session.provider if ctx.session is not None else "")
        )

    def _model(self, ctx: EventContext) -> str:
        metadata = self.state.metadata.value
        return str(
            metadata.model
            or (ctx.settings.model if ctx.settings is not None else "")
        )

    def _record_end(
        self,
        proposal: CompactionProposal,
        *,
        error: str = "",
    ) -> None:
        data = {"compaction_id": str(proposal["compaction_id"])}
        if error:
            data["error"] = error
        self.state.history.record("compaction/end", data, durable=True)

    def _close_stale_compactions(self) -> None:
        """Close durable transactions a crash left without an end marker.

        The surface is a fold of the trajectory, so a crashed attempt has
        either replaced it or not; closing the bracket keeps the transaction
        ledger truthful instead of blocking every later compaction.
        """
        for compaction_id in sorted(
            self.state.history.open_transactions(COMPACTION_TRANSACTION)
        ):
            logger.warning(
                "compaction transaction %s has no end marker; closing it as aborted",
                compaction_id,
            )
            self.state.history.record("compaction/end", durable=True, data={
                "compaction_id": compaction_id,
                "error": "aborted: the failed attempt recorded no end marker",
            })

    async def _compact(
        self,
        ctx: EventContext,
        messages: list[Message],
        *,
        reason: CompactionReason,
        context_tokens_before: int,
        estimate_source: str,
        request_estimate: int | None = None,
        context_limit: int | None = None,
        max_context_tokens: int | None = None,
        output_reservation: int | None = None,
        summary_output_tokens: int | None = None,
        stable_prefix: Message | Sequence[Message] | None = None,
        tools: Sequence[Tool] = (),
        removable_estimate: int | None = None,
    ) -> CompactionProposal | None:
        self._close_stale_compactions()
        if stable_prefix is None:
            stable: Sequence[Message] = ()
        elif isinstance(stable_prefix, Message):
            stable = (stable_prefix,)
        else:
            stable = tuple(stable_prefix)
        source_node_ids = self.state.history.node_ids()
        model = self._active_model(ctx)
        proposal = await build_compaction_proposal(
            model=model,
            record_usage=self._record_auxiliary_usage,
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
            summary_output_tokens=(
                self._summary_output_tokens
                if summary_output_tokens is None else summary_output_tokens
            ),
            stable_prefix=stable,
            tools=tools,
            removable_estimate=removable_estimate,
            record_trajectory=self.state.history.record,
        )
        if proposal is not None:
            prefix_end = int(proposal["prefix_end"])
            proposal["source_node_ids"] = source_node_ids[:prefix_end]
            if self.state.history.node_ids()[:prefix_end] != source_node_ids[:prefix_end]:
                compaction_id = str(proposal["compaction_id"])
                self.state.history.record("compaction/end", durable=True, data={
                    "compaction_id": compaction_id,
                    "error": "selected history changed while summarizing",
                })
                raise RuntimeError("Selected history changed while compacting")
        return proposal

    def _active_model(self, ctx: EventContext) -> ModelPort:
        request = ctx.model_request
        if request is not None:
            # This is the provider binding prepared for the active request;
            # retaining it preserves the exact tool-schema envelope used for
            # route/model selection and provider-side prompt accounting.
            return request.llm
        return self.model

    async def _record_auxiliary_usage(self, usage: dict[str, int]) -> None:
        if usage:
            event = await self._usage.add(usage, update_context=False)
            if event is not None:
                await self._publish_runtime_event(ClientEvent(type="usage", data=event))

    async def _publish_runtime_event(self, event: ClientEvent) -> None:
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(client_event=event),
        )

    def _record_committed(
        self,
        reason: CompactionReason,
        metrics: CompactionMetrics,
        session: SessionInfo,
    ) -> None:
        self._compactions += 1
        self._last_reason = reason
        self._last_compaction = metrics
        usage = metrics.model_usage
        logger.info(
            "compaction completed reason=%s turn=%d messages_before=%d "
            "messages_after=%d history_chars_before=%d history_chars_after=%d "
            "context_tokens_before=%d context_tokens_after_estimate=%d "
            "summary_chars=%d input_tokens=%d output_tokens=%d total_tokens=%d",
            reason,
            int(session.turn_count or 0),
            metrics.messages_before,
            metrics.messages_after,
            metrics.history_chars_before,
            metrics.history_chars_after,
            metrics.context_tokens_before,
            metrics.context_tokens_after_estimate,
            metrics.summary_chars,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("total_tokens", 0),
        )

    def diagnostics(self) -> dict[str, JsonValue]:
        return {
            "status": "ready",
            "automatic": self._automatic,
            "output_reservation": self._output_reservation,
            "trigger_ratio": self._trigger_ratio,
            "keep_recent_turns": self._keep_recent_turns,
            "compactions": self._compactions,
            "last_reason": self._last_reason,
            "last_compaction": (
                self._last_compaction.model_dump(mode="json")
                if self._last_compaction is not None
                else {}
            ),
        }


def _context_window(settings: LoopSettings | None) -> int:
    value = settings.context_window if settings is not None else 32_000
    result = int(value)
    return result if result > 0 else 32_000


def _exception_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _finalize_metrics(
    metrics: CompactionMetrics,
    *,
    original_messages: list[Message],
    proposed_messages: list[Message],
    committed_messages: list[Message],
) -> CompactionMetrics:
    predicted_after = metrics.context_tokens_after_estimate
    released = metrics.context_tokens_released_estimate
    if predicted_after > 0 and committed_messages != proposed_messages:
        delta = (
            estimate_messages_tokens(committed_messages)
            - estimate_messages_tokens(proposed_messages)
        )
        predicted_after = max(1, predicted_after + delta)
        released = max(0, metrics.context_tokens_before - predicted_after)
    return metrics.model_copy(update={
        "history_chars_before": history_chars(original_messages),
        "history_chars_after": history_chars(committed_messages),
        "messages_before": len(original_messages),
        "messages_after": len(committed_messages),
        "messages_removed": len(original_messages) - len(committed_messages),
        "context_tokens_after_estimate": predicted_after,
        "context_tokens_released_estimate": released,
    })


__all__ = ["CompactService"]
