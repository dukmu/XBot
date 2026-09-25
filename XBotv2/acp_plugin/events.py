"""Translate XBot runtime events into ACP session updates."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from acp import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
    update_user_message_text,
)
from acp.schema import UsageUpdate
from XBotv2.agentloop.protocol import (
    AssistantCompleted,
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopEvent,
    LoopTurnEnded,
    LoopTurnStarted,
    ToolCallsStarted,
    ToolCompleted,
    TurnCancelled,
    TurnFinished,
    UsageObserved,
    is_loop_event,
)
from XBotv2.core.domain import TokenCounters
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.interactions import ClientNotice
from XBotv2.jobs.protocol import JobCompletedEvent, JobUpdatedEvent
from XBotv2.session import (
    AssistantRecord,
    CompactionSummaryRecord,
    ConversationRecord,
    HumanInputRecord,
    RuntimeNoticeRecord,
    ToolRecord,
)
from XBotv2.core.tools import (
    ToolCancelled,
    ToolDenied,
    ToolFailed,
    ToolOutcome,
    ToolSucceeded,
)


class ACPEventMapper:
    """Stateful event mapper for one ACP prompt turn."""

    def __init__(self, *, context_size: int = 0) -> None:
        self.stop_reason = "end_turn"
        self.error: LoopError | None = None
        self.usage: TokenCounters | None = None
        self._streamed_message = False
        self._streamed_reasoning = False
        self._context_size = context_size
        self._jobs: set[str] = set()

    def updates(
        self,
        event: object,
        *,
        fallback_context_size: int | None = None,
    ) -> list[Any]:
        if isinstance(event, LoopTurnStarted):
            self._streamed_message = False
            self._streamed_reasoning = False
            return []
        if isinstance(event, AssistantTextDelta):
            self._streamed_message = True
            return [update_agent_message_text(event.text)]
        if isinstance(event, AssistantReasoningDelta):
            self._streamed_reasoning = True
            return [update_agent_thought_text(event.text)]
        if isinstance(event, AssistantCompleted):
            updates: list[Any] = []
            if not self._streamed_reasoning:
                reasoning = "".join(
                    part.text
                    for part in event.message.parts
                    if isinstance(part, ReasoningPart)
                )
                if reasoning:
                    updates.append(update_agent_thought_text(reasoning))
            if not self._streamed_message:
                content = "".join(
                    part.text
                    for part in event.message.parts
                    if isinstance(part, TextPart)
                )
                if content:
                    updates.append(update_agent_message_text(content))
            return updates
        if isinstance(event, ClientNotice):
            return [update_agent_message_text(event.message)]
        if isinstance(event, ToolCallsStarted):
            self._streamed_message = False
            return [
                start_tool_call(
                    str(started.call.id),
                    started.call.name,
                    # The tool owner declares the category upstream; the
                    # carrier renders it and never re-derives a taxonomy.
                    kind=started.category or None,
                    status="pending",
                    raw_input=started.call.args,
                )
                for started in event.calls
            ]
        if isinstance(event, ToolCompleted):
            message = event.execution.message
            outcome = message.outcome
            content = _outcome_text(outcome)
            return [update_tool_call(
                str(message.call.id),
                status="completed" if isinstance(outcome, ToolSucceeded) else "failed",
                content=[tool_content(text_block(content))],
                raw_output=outcome.model_dump(mode="json"),
            )]
        if isinstance(event, (JobUpdatedEvent, JobCompletedEvent)):
            view = event.view
            title = view.label
            content = (
                [tool_content(text_block(view.summary))]
                if view.summary else None
            )
            raw_output = view.model_dump(mode="json")
            if view.id not in self._jobs:
                self._jobs.add(view.id)
                return [start_tool_call(
                    view.id,
                    title,
                    kind="execute" if view.kind == "shell" else "other",
                    status=_task_status(view.state),
                    content=content,
                    raw_output=(
                        raw_output
                        if view.state not in {"queued", "running"}
                        else None
                    ),
                )]
            return [update_tool_call(
                view.id,
                status=_task_status(view.state),
                content=content,
                raw_output=raw_output,
            )]
        if isinstance(event, UsageObserved):
            current = event.usage.counters
            if self.usage is None:
                self.usage = current
            else:
                previous = self.usage
                self.usage = TokenCounters(
                    input=previous.input + current.input,
                    output=previous.output + current.output,
                    cache_read=previous.cache_read + current.cache_read,
                    cache_create=previous.cache_create + current.cache_create,
                    prompt_cache_write=(
                        previous.prompt_cache_write + current.prompt_cache_write
                    ),
                )
            size = fallback_context_size or self._context_size
            return [UsageUpdate(
                session_update="usage_update",
                used=current.input,
                size=size,
            )] if size > 0 else []
        if isinstance(event, LoopTurnEnded):
            if isinstance(event.outcome, TurnCancelled):
                self.stop_reason = "cancelled"
            elif isinstance(event.outcome, TurnFinished):
                self.stop_reason = event.outcome.stop_reason
            return []
        if isinstance(event, LoopError):
            self.error = event
            return []
        if is_loop_event(event):
            return []
        return []


def replay_history(items: Iterable[ConversationRecord]) -> list[Any]:
    """Translate persisted conversation messages into ACP load updates."""
    updates: list[Any] = []
    for index, item in enumerate(items):
        if isinstance(item, RuntimeNoticeRecord) and item.content:
                source, event = item.source, item.event
                updates.append(start_tool_call(
                    item.id or f"runtime-input-{index}",
                    f"Injected context · {source} / {event}",
                    kind="other",
                    status="completed",
                    content=[tool_content(text_block(item.content))],
                    raw_output={
                        "source": source,
                        "event": event,
                        "content": item.content,
                    },
                ))
                continue
        elif isinstance(item, HumanInputRecord) and item.content:
            updates.append(update_user_message_text(item.content))
            continue
        if isinstance(item, AssistantRecord):
            if item.reasoning:
                updates.append(update_agent_thought_text(item.reasoning))
            if item.content:
                updates.append(update_agent_message_text(item.content))
            for call in item.tool_calls:
                # Replay has no registry to consult, so it does not invent a
                # category: the client renders the call without one.
                updates.append(start_tool_call(
                    call.id,
                    call.name,
                    status="pending",
                    raw_input=call.args,
                ))
            continue
        if isinstance(item, CompactionSummaryRecord):
            updates.append(update_agent_message_text(item.summary))
            continue
        if not isinstance(item, ToolRecord):
            continue
        outcome = item.outcome
        content = _outcome_text(outcome)
        updates.append(update_tool_call(
            item.call.id,
            status="completed" if isinstance(outcome, ToolSucceeded) else "failed",
            content=[tool_content(text_block(content))],
            raw_output=outcome.model_dump(mode="json"),
        ))
    return updates


def _outcome_text(outcome: ToolOutcome) -> str:
    if isinstance(outcome, (ToolSucceeded, ToolFailed)):
        return "".join(
            part.text
            for part in outcome.output.parts
            if isinstance(part, TextPart)
        )
    if isinstance(outcome, (ToolDenied, ToolCancelled)):
        return outcome.reason
    raise TypeError(f"Unsupported tool outcome: {type(outcome).__name__}")


def _task_status(status: str) -> str:
    if status == "queued":
        return "pending"
    if status == "succeeded":
        return "completed"
    if status.startswith("failed") or status.startswith("cancelled"):
        return "failed"
    return "in_progress"


__all__ = ["ACPEventMapper", "replay_history"]
