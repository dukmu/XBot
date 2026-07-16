"""Conversation history compaction plugin."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from xbotv2.api import (
    Command,
    CommandResult,
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
    Message,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)

logger = logging.getLogger("xbotv2.compact")


class CompactPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._automatic = True
        self._trigger_chars = 80_000
        self._output_reservation = 4_096
        self._trigger_ratio = 0.8
        self._keep_recent_turns = 4
        self._summary_max_chars = 8_000
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""

    async def on_load(self, config: dict[str, Any]) -> None:
        self._automatic = bool(config.get("automatic", True))
        self._trigger_chars = int(config.get("trigger_chars", 80_000))
        self._output_reservation = int(config.get("output_reservation", 4_096))
        self._trigger_ratio = float(config.get("trigger_ratio", 0.8))
        self._keep_recent_turns = int(config.get("keep_recent_turns", 4))
        self._summary_max_chars = int(config.get("summary_max_chars", 8_000))

    async def on_unload(self) -> None:
        self._manual_requested = False
        self._compactions = 0
        self._last_reason = ""

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.BEFORE_CONTEXT, self._on_before_context)
        ctx.register_hook(HookStage.BEFORE_TOOL_CALL, self._allow_compact)

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

        ctx.register_tool(
            Tool.from_function(request_compaction, name="compact"),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:compact",
            ),
        )
        ctx.register_command(Command(
            name="compact",
            description="Compact conversation history immediately while idle.",
            handler=self._compact_command,
            usage="/compact",
            examples=("/compact",),
        ))

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
                compacted = await ctx.engine.run_context_maintenance()
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
        return CommandResult(
            "Conversation history compacted.",
            data={"requested": True, "compacted": True},
        )

    async def _allow_compact(self, ctx: HookContext):
        if ctx.tool_call is not None and ctx.tool_call.name == "compact":
            return HookDecision(
                HookAction.ALLOW,
                "Compaction requests are pre-approved by the Compact plugin",
            )

    async def _on_before_context(self, ctx: HookContext):
        messages = list(ctx.state.get("messages") or [])
        manual = self._manual_requested
        turn = ctx.session.turn_count
        provider_input = _latest_provider_input_tokens(messages)
        max_context = int(getattr(ctx.config, "max_context_tokens", 32_000))
        token_trigger = int(
            max(1, max_context - self._output_reservation) * self._trigger_ratio
        )
        history_chars = _history_chars(messages)
        threshold_reached = (
            provider_input is not None and provider_input >= token_trigger
        ) or history_chars >= self._trigger_chars
        automatic = self._automatic and threshold_reached
        if not manual and not automatic:
            return None

        split = _compact_prefix_end(messages, self._keep_recent_turns)
        if split == 0:
            self._manual_requested = False
            return None
        if ctx.invoke_model is None:
            raise RuntimeError("CompactPlugin requires HookContext.invoke_model")

        reason = "manual" if manual else "automatic"
        self._manual_requested = False
        logger.info(
            "compaction started reason=%s turn=%d messages=%d history_chars=%d",
            reason,
            turn,
            len(messages),
            history_chars,
        )
        try:
            response = await ctx.invoke_model(
                _summary_request(messages[:split], self._summary_max_chars)
            )
            if response.tool_calls:
                raise RuntimeError("Compaction model must not call tools")
            summary = response.content.strip()
            if not summary:
                raise RuntimeError("Compaction model returned an empty summary")
        except asyncio.CancelledError:
            raise
        except Exception:
            if manual:
                raise
            logger.exception(
                "automatic compaction failed; continuing with original history"
            )
            return None
        summary = _strip_summary_heading(summary)[:self._summary_max_chars]
        compacted = Message(
            role="system",
            content=f"## Conversation Summary\n\n{summary}",
        )
        self._compactions += 1
        self._last_reason = reason
        logger.info(
            "compaction completed reason=%s turn=%d messages_before=%d messages_after=%d",
            reason,
            turn,
            len(messages),
            1 + len(messages[split:]),
        )
        return {
            "messages": [compacted, *messages[split:]],
            "compact_reason": reason,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "automatic": self._automatic,
            "trigger_chars": self._trigger_chars,
            "output_reservation": self._output_reservation,
            "trigger_ratio": self._trigger_ratio,
            "keep_recent_turns": self._keep_recent_turns,
            "compactions": self._compactions,
            "last_reason": self._last_reason,
        }


def _history_chars(messages: list[Message]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.content or ""))
        for call in message.tool_calls or []:
            total += len(call.name) + len(str(call.args))
    return total


def _latest_provider_input_tokens(messages: list[Message]) -> int | None:
    for message in reversed(messages):
        usage = message.usage_metadata or {}
        if "context_tokens" in usage or "input_tokens" in usage:
            value = int(
                usage.get("context_tokens") or usage.get("input_tokens") or 0
            )
            return value if value > 0 else None
    return None


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


def _summary_request(messages: list[Message], max_chars: int) -> list[Message]:
    instruction = (
        "Summarize the conversation for a future agent. Preserve user requirements, "
        "decisions, file paths, commands, tool outcomes, errors, and unresolved work. "
        "Do not continue the task or call tools. Return only the summary, using no more "
        f"than {max_chars} characters."
    )
    return [
        Message(role="system", content=instruction),
        *messages,
        Message(role="user", content="Produce the conversation summary now."),
    ]


def _strip_summary_heading(summary: str) -> str:
    heading = "## Conversation Summary"
    while summary.startswith(heading):
        summary = summary[len(heading):].lstrip(" \r\n")
    return summary
