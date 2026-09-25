"""Compact plugin runtime service and history-commit ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, Sequence
from pydantic import JsonValue

from XBotv2.application import RUNTIME_EVENT, RuntimeEvent
from XBotv2.core import (
    ProviderFailure,
    context_token_limit,
    estimate_messages_tokens,
    estimate_model_request_tokens,
    estimate_request_tokens,
)
from XBotv2.core.messages import ConversationMessage
from XBotv2.core.domain import (
    ResolvedModelSelection,
    RequestObservation,
    TransactionAborted,
    TransactionCommitted,
    TransactionEnded,
    TransactionFailed,
    TransactionOutcome,
    TransactionRef,
    UsageDelta,
)
from XBotv2.agentloop import Events, LoopState
from XBotv2.agentloop.events import (
    BeforeContextBuild,
    KeepContextRequest,
    ModelRequestReady,
    ModelResponseObserved,
    OnModelFailure,
    ReplaceContextRequest,
    RetryRequest,
    TurnEnded,
)
from XBotv2.commands import CommandResult
from XBotv2.llm.contracts import ModelPort
from XBotv2.session.contracts import (
    HISTORY_CHANGED,
    HistoryChanged,
    SessionRuntimeState,
)

from XBotv2.compact.commands import run_compact_command
from XBotv2.compact.compactor import build_compaction_plan
from XBotv2.compact.contracts import CompactConfig
from XBotv2.compact.contracts import CompactionPlan
from XBotv2.compact.protocol import (
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
from XBotv2.compact.protocol import CompactionCompleted, CompactionFailed

logger = logging.getLogger("xbotv2.compact")


class CompactEventsPort(Protocol):
    async def serial(self, event: str, *args: object) -> object: ...

    async def emit(self, event: str, *args: object) -> None: ...


class UsagePort(Protocol):
    async def record(
        self,
        observation: RequestObservation,
        usage: UsageDelta,
    ): ...


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
    ) -> tuple[bool, CompactionMetrics | None]:
        messages = list(self.state.messages)
        proposal = await self._compact(
            messages,
            reason="manual",
            selection=self.state.metadata.value.runtime_selection.model,
            context_tokens_before=estimate_messages_tokens(messages),
            estimate_source="estimated_history",
        )
        result = await self._commit(proposal)
        metrics = proposal.metrics if proposal is not None and result else None
        return result, metrics

    async def _on_before_context(self, event: BeforeContextBuild):
        manual = self._consume_manual_request()
        if not manual:
            return None
        request = event.request
        messages = list(self.state.messages)
        proposal = await self._compact(
            messages,
            reason="manual",
            selection=event.request.runtime_selection.model,
            context_tokens_before=estimate_messages_tokens(messages),
            estimate_source="estimated_history",
            request_estimate=estimate_request_tokens(messages, ()),
            stable_prefix=leading_system_messages(messages),
        )
        committed = await self._commit(proposal)
        if not committed:
            return KeepContextRequest()
        return ReplaceContextRequest(
            replace(request, history=tuple(self.state.messages))
        )

    async def _on_model_request_ready(self, event: ModelRequestReady):
        if not self._automatic:
            return None
        request = event.request
        selection = request.selection
        context_tokens = estimate_model_request_tokens(request)
        output_reservation = (
            self._output_reservation
            if self._output_reservation is not None
            else max(0, int(selection.generation.max_output_tokens))
        )
        context_limit = context_token_limit(
            selection.context_window,
            trigger_ratio=self._trigger_ratio,
            output_reservation=output_reservation,
        )
        if context_tokens < context_limit:
            return None
        proposal = await self._compact(
            list(self.state.messages),
            reason="automatic",
            selection=selection,
            context_tokens_before=context_tokens,
            estimate_source="estimated_request",
            request_estimate=context_tokens,
            context_limit=context_limit,
            max_context_tokens=selection.context_window,
            output_reservation=output_reservation,
            summary_output_tokens=self._summary_output_tokens,
        )
        if proposal is not None:
            await self._commit(proposal)
        # The engine observes the changed history revision after observers and
        # rebuilds this candidate request from the compacted surface.
        return None

    async def _on_model_request_error(self, event: OnModelFailure):
        """Recover one provider-confirmed context overflow from durable history."""
        session = self.state.session
        owner = (session.session_id, session.thread_id)
        if owner != self._overflow_owner:
            self._overflow_owner = owner
            self._overflow_retries = 0
        if (
            not self._automatic
            or not isinstance(event.error, ProviderFailure)
            or event.error.error.category != "context_overflow"
            or self._overflow_retries >= 1
        ):
            return None
        messages = list(self.state.messages)
        request = event.request
        source_ids = self.state.history.ids()
        max_context = request.selection.context_window
        context_tokens = estimate_model_request_tokens(request)
        request_estimate = context_tokens
        estimate_source = "estimated_request"
        # The request's bound model is the active route/model at this exact
        # step.  The plugin's startup injection may point at an old provider.
        try:
            proposal = await self._compact(
                messages,
                reason="context-overflow",
                selection=request.selection,
                context_tokens_before=context_tokens,
                estimate_source=estimate_source,
                request_estimate=request_estimate,
                context_limit=max_context,
                max_context_tokens=max_context,
                output_reservation=request.selection.generation.max_output_tokens,
                summary_output_tokens=self._summary_output_tokens,
                stable_prefix=leading_system_messages(request.messages),
            )
            if proposal is None:
                return None
            result = await self._commit(proposal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "context-overflow compaction failed; preserving provider error"
            )
            return None
        if (
            result
            and self.state.history.ids() != source_ids
        ):
            self._overflow_retries += 1
            return RetryRequest(request)
        return None

    async def _on_after_model_response(self, _ctx: ModelResponseObserved) -> None:
        self._overflow_retries = 0

    async def _on_turn_end(self, _ctx: TurnEnded) -> None:
        # A completed turn is the idle boundary for overflow recovery budget.
        self._overflow_retries = 0

    async def _commit(
        self,
        proposal: CompactionPlan | None,
    ) -> bool:
        """Commit one immutable plan at the history ownership boundary."""
        if proposal is None:
            return False

        original_messages = list(self.state.messages)
        if proposal.selection.expected_revision != self.state.history.surface_revision:
            message = "Selected history changed while compacting"
            self._record_end(proposal, TransactionFailed(error=message))
            raise RuntimeError(message)
        source_ids = tuple(str(value) for value in proposal.selection.source_ids)
        if self.state.history.ids()[:len(source_ids)] != source_ids:
            message = "Selected history changed while compacting"
            self._record_end(proposal, TransactionFailed(error=message))
            raise RuntimeError(message)

        session = self.state.session
        pre = BeforeCompact(plan=proposal, session=session)
        try:
            pre_result = await self._events.serial(PRE_COMPACT, pre)
        except asyncio.CancelledError:
            message = "Compaction cancelled."
            self._record_end(proposal, TransactionAborted(reason="cancelled"))
            await self._publish_runtime_event(CompactionFailed(
                reason=proposal.reason,
                message=message,
                automatic=is_automatic_compaction(proposal.reason),
            ))
            raise
        except BaseException as exc:
            self._record_end(
                proposal,
                TransactionFailed(error=_exception_text(exc)),
            )
            raise

        if pre_result is not None:
            message = "Compaction was rejected before commit."
            self._record_end(proposal, TransactionFailed(error=message))
            await self._publish_runtime_event(CompactionFailed(
                reason=proposal.reason,
                message=message,
                automatic=is_automatic_compaction(proposal.reason),
            ))
            raise RuntimeError(message)

        metrics = proposal.metrics
        prefix_end = len(source_ids)
        previous_count = len(original_messages)
        try:
            self.state.replace_message_range(
                0,
                prefix_end,
                [proposal.summary],
                operation=f"compact:{proposal.id}",
                preserve_transcript=True,
            )
            current_messages = list(self.state.messages)
            committed = AfterCompact(
                plan=proposal,
                messages=tuple(current_messages),
                session=session,
                previous_message_count=previous_count,
                current_message_count=len(current_messages),
            )
            await self._events.emit(POST_COMPACT, committed)
            await self._events.emit(
                HISTORY_CHANGED,
                HistoryChanged(
                    tuple(current_messages),
                    operation=f"compact:{proposal.reason}",
                ),
            )
        except BaseException as exc:
            self._record_end(
                proposal,
                TransactionFailed(error=_exception_text(exc)),
            )
            raise
        self._record_end(proposal, TransactionCommitted())

        self._record_committed(proposal.reason, metrics, session)
        await self._publish_runtime_event(CompactionCompleted(
            reason=proposal.reason,
            metrics=metrics,
            automatic=is_automatic_compaction(proposal.reason),
            summary=proposal.summary,
        ))
        return True

    def _record_end(
        self,
        proposal: CompactionPlan,
        outcome: TransactionOutcome,
    ) -> None:
        transaction = TransactionRef(
            kind="compaction", id=proposal.id
        )
        self.state.history.record(
            TransactionEnded(transaction=transaction, outcome=outcome),
            durable=True,
        )

    def _close_stale_compactions(self) -> None:
        """Close durable transactions a crash left without an end marker.

        The surface is a fold of the trajectory, so a crashed attempt has
        either replaced it or not; closing the bracket keeps the transaction
        ledger truthful instead of blocking every later compaction.
        """
        for compaction_id in sorted(
            self.state.history.open_transactions("compaction")
        ):
            logger.warning(
                "compaction transaction %s has no end marker; closing it as aborted",
                compaction_id,
            )
            self.state.history.record(
                TransactionEnded(
                    transaction=TransactionRef(
                        kind="compaction", id=compaction_id
                    ),
                    outcome=TransactionAborted(
                        reason="the failed attempt recorded no end marker"
                    ),
                ),
                durable=True,
            )

    async def _compact(
        self,
        messages: list[ConversationMessage],
        *,
        reason: CompactionReason,
        selection: ResolvedModelSelection,
        context_tokens_before: int,
        estimate_source: str,
        request_estimate: int | None = None,
        context_limit: int | None = None,
        max_context_tokens: int | None = None,
        output_reservation: int | None = None,
        summary_output_tokens: int | None = None,
        stable_prefix: Sequence[ConversationMessage] | None = None,
        removable_estimate: int | None = None,
    ) -> CompactionPlan | None:
        self._close_stale_compactions()
        if stable_prefix is None:
            stable: Sequence[ConversationMessage] = ()
        else:
            stable = tuple(stable_prefix)
        source_ids = self.state.history.ids()
        proposal = await build_compaction_plan(
            model=self.model,
            selection=selection,
            record_usage=self._record_auxiliary_usage,
            publish_runtime_event=self._publish_runtime_event,
            session=self.state.session,
            messages=messages,
            history_revision=self.state.history.surface_revision,
            source_ids=source_ids,
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
            removable_estimate=removable_estimate,
            record_trajectory=lambda event: self.state.history.record(
                event, durable=True
            ),
        )
        return proposal

    async def _record_auxiliary_usage(
        self,
        observation: RequestObservation,
        usage: UsageDelta,
    ) -> None:
        await self._usage.record(observation, usage)

    async def _publish_runtime_event(self, event) -> None:
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(event=event),
        )

    def _record_committed(
        self,
        reason: CompactionReason,
        metrics: CompactionMetrics,
        session: SessionRuntimeState,
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
            usage.input,
            usage.output,
            usage.input + usage.output,
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


def _exception_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


__all__ = ["CompactService"]
