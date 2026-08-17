"""Conversation history compaction plugin."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from XBotv2.core import (
    Command,
    CommandResult,
    EventContext,
    Events,
    Message,
    MESSAGE_FORMAT_KEY,
    Tool,
    ToolResult,
    calibrated_context_tokens,
    context_token_limit,
    estimate_messages_tokens,
    prompt_container,
    prompt_element,
)
from xcore import S

logger = logging.getLogger("xbotv2.compact")


class CompactPlugin:
    inject = ['tools', 'commands', 'model']
    name = "compact"
    Config = S.object({
        "automatic": S.boolean().optional(),
        "output_reservation": S.number().optional(),
        "trigger_ratio": S.number().optional(),
        "keep_recent_turns": S.number().optional(),
        "summary_max_chars": S.number().optional(),
    })

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

    def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        self.model = ctx.model
        self.store = ctx.state.namespace("compact")
        config = config or {}
        self._automatic = bool(config.get("automatic", True))
        reservation = config.get("output_reservation")
        self._output_reservation = (
            int(reservation) if reservation is not None else None
        )
        self._trigger_ratio = float(config.get("trigger_ratio", 0.8))
        self._keep_recent_turns = int(config.get("keep_recent_turns", 4))
        self._summary_max_chars = int(config.get("summary_max_chars", 8_000))
        ctx.dispose(self._on_unload)
        ctx.on(Events.BEFORE_CONTEXT, self._on_before_context)
        ctx.on(
            Events.BEFORE_MODEL_REQUEST,
            self._on_before_model_request,
        )

        async def request_compaction() -> ToolResult:
            """Request one semantic compaction before the next model call.

            Use this when older conversation detail is consuming context but the
            task must continue. It summarizes an old completed prefix, preserves
            recent turns, and does not complete the current task. Do not call it
            repeatedly when automatic compaction is already active.
            """
            self._manual_requested = True
            return ToolResult.success(
                "Conversation compaction requested.",
                data={"requested": True},
            )

        ctx.tools.register(
            Tool.from_function(request_compaction, name="compact"),
        )
        ctx.commands.register(Command(
            name="compact",
            description="Compact conversation history immediately while idle.",
            handler=self._compact_command,
            usage="/compact",
            examples=("/compact",),
        ))

    async def _on_unload(self) -> None:
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""
        self._last_compaction = {}

    async def _compact_command(self, ctx: Any, raw_args: str) -> CommandResult:
        if raw_args.strip():
            return CommandResult(
                "Usage: /compact",
                status="error",
                data={"requested": False},
            )
        await ctx.turn_lock.acquire()
        try:
            self._manual_requested = True
            try:
                result = await self._on_before_context(EventContext(
                    messages=ctx.engine.messages,
                    config=ctx.engine.settings,
                    session=ctx.engine.session,
                ))
                compacted = bool(result and result.get("rebuild"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return CommandResult(
                    f"Conversation compaction failed: {exc}",
                    status="error",
                    data={"requested": False},
                )
        finally:
            ctx.turn_lock.release()
        if not compacted:
            return CommandResult(
                "Conversation history is too short to compact.",
                data={"requested": False, "compacted": False},
            )
        metrics = dict(self._last_compaction)
        data: dict[str, Any] = {"requested": True, "compacted": True}
        if metrics:
            data["metrics"] = metrics
        return CommandResult(
            _compact_result_message(metrics),
            data=data,
        )

    async def _on_before_context(self, ctx: EventContext):
        if not self._manual_requested:
            return None
        messages = list(ctx.messages)
        self._manual_requested = False
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
        max_context = int(getattr(ctx.config, "max_context_tokens", 32_000))
        context_tokens, request_estimate, estimate_source = (
            calibrated_context_tokens(
                context_messages,
                tools,
                messages,
                provider=str(getattr(ctx.session, "provider", "") or ""),
                context_window=max_context,
            )
        )
        configured_output = int(
            getattr(ctx.config, "max_output_tokens", 0) or 0
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

        stable_prefix = next(
            (
                message
                for message in context_messages
                if getattr(message, "role", "") == "system"
            ),
            None,
        )
        split = _compact_prefix_end(messages, self._keep_recent_turns)
        provider_history = [
            message
            for message in context_messages
            if getattr(message, "role", "") != "system"
        ]
        prefix_messages = messages[:split]
        provider_prefix_count = sum(
            message.role != "system" for message in prefix_messages
        )
        removable_estimate = estimate_messages_tokens(
            provider_history[:provider_prefix_count]
        ) + estimate_messages_tokens([
            message
            for message in prefix_messages
            if message.role == "system"
        ])
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
            stable_prefix=stable_prefix,
            removable_estimate=removable_estimate,
        )
        return await self._commit(ctx, proposal)

    async def _commit(
        self,
        ctx: EventContext,
        proposal: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Own the complete compaction transaction outside the loop."""
        if proposal is None:
            return None
        reason = str(proposal.get("compact_reason") or "context")
        messages = list(proposal["messages"])
        pre = EventContext(
            messages=messages,
            session=ctx.session,
            event={"reason": reason},
        )
        pre_result = await self.ctx.serial(Events.PRE_COMPACT, pre)
        if isinstance(pre_result, dict):
            messages = list(pre_result.get("messages", messages))
            reason = str(pre_result.get("compact_reason") or reason)
        elif pre_result is not None:
            return {
                "event": {
                    "type": "error",
                    "data": {
                        "code": "hook_rejected",
                        "message": "Compaction was rejected before commit.",
                        "stage": Events.PRE_COMPACT,
                    },
                },
                "turn_complete": True,
            }

        previous_count = len(ctx.messages)
        ctx.messages[:] = messages
        committed = EventContext(
            messages=ctx.messages,
            session=ctx.session,
            event={
                "reason": reason,
                "metrics": dict(proposal.get("compact_metrics") or {}),
                "previous_message_count": previous_count,
                "current_message_count": len(ctx.messages),
                "history_operation": (f"compact:{reason}", 0),
            },
        )
        await self.ctx.emit(Events.POST_COMPACT, committed)
        await self.ctx.emit(Events.STATE_CHANGED, committed)
        await self._publish_runtime_event({
            "type": "compaction_completed",
            "data": {
                "reason": reason,
                "metrics": committed.event["metrics"],
            },
        })
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
        stable_prefix: Message | None = None,
        removable_estimate: int | None = None,
    ):
        split = _compact_prefix_end(messages, self._keep_recent_turns)
        if split == 0:
            return None
        llm = self.model

        turn = ctx.session.turn_count
        history_chars = _history_chars(messages)
        logger.info(
            "compaction started reason=%s turn=%d messages=%d history_chars=%d "
            "context_tokens=%d context_limit=%s estimate_source=%s",
            reason,
            turn,
            len(messages),
            history_chars,
            context_tokens_before,
            context_limit,
            estimate_source,
        )
        await self._publish_runtime_event({
            "type": "compaction_started",
            "data": {
                "reason": reason,
                "messages_before": len(messages),
                "history_chars_before": history_chars,
                "context_tokens_before": context_tokens_before,
                "context_limit": context_limit,
            },
        })
        try:
            summary_messages = messages[:split]
            if stable_prefix is not None:
                summary_messages = [
                    message
                    for message in summary_messages
                    if message.role != "system"
                ]
            response = await _invoke_llm(
                llm,
                _summary_request(
                    summary_messages,
                    self._summary_max_chars,
                    stable_prefix=stable_prefix,
                )
            )
            if response.tool_calls:
                raise RuntimeError("Compaction model must not call tools")
            summary = response.content.strip()
            if not summary:
                raise RuntimeError("Compaction model returned an empty summary")
        except asyncio.CancelledError:
            await self._publish_runtime_event({
                "type": "compaction_failed",
                "data": {"reason": reason, "message": "Compaction cancelled."},
            })
            raise
        except Exception as exc:
            await self._publish_runtime_event({
                "type": "compaction_failed",
                "data": {"reason": reason, "message": str(exc)},
            })
            if reason == "manual":
                raise
            logger.exception(
                "automatic compaction failed; continuing with original history"
            )
            return None
        summary = _strip_summary_heading(summary)
        summary, summary_truncated = _limit_summary(
            summary,
            self._summary_max_chars,
        )
        compacted = Message(
            role="system",
            content=prompt_container(
                "historical_context",
                [
                    prompt_element(
                        "conversation_summary",
                        summary,
                        attributes={"reason": reason},
                    ),
                ],
                attributes={"source": "compaction"},
            ),
            additional_kwargs={MESSAGE_FORMAT_KEY: "xml"},
        )
        compacted_messages = [compacted, *messages[split:]]
        usage = _model_usage(response.usage_metadata)
        removed_estimate = (
            removable_estimate
            if removable_estimate is not None
            else estimate_messages_tokens(messages[:split])
        )
        summary_estimate = estimate_messages_tokens([compacted])
        context_tokens_after = max(
            1,
            context_tokens_before - removed_estimate + summary_estimate,
        )
        if reason == "automatic" and context_tokens_after >= context_tokens_before:
            message = (
                "Automatic compaction would not reduce the estimated context; "
                "the retained prefix or tool schemas dominate the request."
            )
            logger.warning(message)
            await self._publish_runtime_event({
                "type": "compaction_failed",
                "data": {"reason": reason, "message": message},
            })
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
            "history_chars_before": history_chars,
            "history_chars_after": _history_chars(compacted_messages),
            "summary_chars": len(summary),
            "summary_truncated": summary_truncated,
            "messages_before": len(messages),
            "messages_after": len(compacted_messages),
            "messages_removed": len(messages) - len(compacted_messages),
            "model_usage": usage,
        }
        self._compactions += 1
        self._last_reason = reason
        self._last_compaction = metrics
        logger.info(
            "compaction completed reason=%s turn=%d messages_before=%d "
            "messages_after=%d history_chars_before=%d history_chars_after=%d "
            "context_tokens_before=%d context_tokens_after_estimate=%d "
            "summary_chars=%d input_tokens=%d output_tokens=%d total_tokens=%d",
            reason,
            turn,
            metrics["messages_before"],
            metrics["messages_after"],
            metrics["history_chars_before"],
            metrics["history_chars_after"],
            metrics["context_tokens_before"],
            metrics["context_tokens_after_estimate"],
            metrics["summary_chars"],
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
        )
        return {
            "messages": compacted_messages,
            "compact_reason": reason,
            "compact_metrics": metrics,
        }

    async def _publish_runtime_event(self, event: dict[str, Any]) -> None:
        await self.ctx.emit(
            Events.RUNTIME_EVENT,
            EventContext(client_event=event),
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


def _history_chars(messages: list[Message]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.content or ""))
        for call in message.tool_calls or []:
            total += len(call.name) + len(str(call.args))
    return total


def _model_usage(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(
            usage.get("total_tokens") or input_tokens + output_tokens
        ),
        "context_tokens": int(usage.get("context_tokens") or input_tokens),
    }
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if usage.get(key) is not None:
            result[key] = int(usage[key])
    return result


def _compact_result_message(metrics: dict[str, Any]) -> str:
    if not metrics:
        return "Conversation history compacted."
    usage = metrics.get("model_usage") or {}
    return (
        "Conversation history compacted "
        f"from about {metrics.get('context_tokens_before', 0)} to "
        f"{metrics.get('context_tokens_after_estimate', 0)} context tokens; "
        f"summary model used {usage.get('input_tokens', 0)} input and "
        f"{usage.get('output_tokens', 0)} output tokens."
    )


def _compact_prefix_end(messages: list[Message], keep_recent_turns: int) -> int:
    user_indexes = [
        index
        for index, message in enumerate(messages)
        if message.role == "user"
    ]
    if len(user_indexes) > keep_recent_turns:
        return user_indexes[-keep_recent_turns]

    assistant_indexes = [
        index
        for index, message in enumerate(messages)
        if message.role == "assistant"
    ]
    if len(assistant_indexes) > keep_recent_turns:
        return assistant_indexes[-keep_recent_turns]
    return 0


def _summary_request(
    messages: list[Message],
    max_chars: int,
    *,
    stable_prefix: Message | None = None,
) -> list[Message]:
    instruction = (
        "Summarize the supplied older conversation for future continuation. "
        "Preserve the current objective and constraints, user corrections and accepted "
        "decisions, verified state and essential evidence, unresolved problems, and "
        "remaining work. Include paths or errors only when needed to continue. "
        "Distinguish verified facts and completed work from plans or unverified claims. "
        "Omit repetition, superseded discussion, raw logs, and recoverable detail. "
        "Do not continue the task or call tools. Return only concise Markdown using no "
        f"more than {max_chars} characters."
    )
    request = [
        Message(
            role="system",
            content=prompt_element("summary_instructions", instruction),
        ),
        *messages,
        Message(
            role="user",
            content=prompt_element(
                "summary_request",
                "Produce the conversation summary now.",
            ),
        ),
    ]
    if stable_prefix is not None:
        request.insert(0, stable_prefix)
    return request


def _strip_summary_heading(summary: str) -> str:
    heading = "## Conversation Summary"
    while summary.startswith(heading):
        summary = summary[len(heading):].lstrip(" \r\n")
    return summary


def _limit_summary(summary: str, max_chars: int) -> tuple[str, bool]:
    if len(summary) <= max_chars:
        return summary, False
    marker = "\n\n[Middle of overlong summary omitted]\n\n"
    remaining = max(0, max_chars - len(marker))
    head = remaining * 2 // 3
    tail = remaining - head
    return summary[:head].rstrip() + marker + summary[-tail:].lstrip(), True


async def _invoke_llm(llm: Any, messages: list[Any]) -> Any:
    """Run one unbound auxiliary model call for compaction."""
    from XBotv2.core.messages import merge_model_chunk

    aggregate: Any = None
    async for chunk in llm.astream(messages):
        aggregate = merge_model_chunk(aggregate, chunk)
    if aggregate is None:
        raise RuntimeError("Compaction model produced no response")
    return aggregate


plugin = CompactPlugin()
