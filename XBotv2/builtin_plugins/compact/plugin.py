"""Conversation history compaction plugin."""

from __future__ import annotations

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
        self._last_auto_turn = -1
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
        self._last_auto_turn = -1
        self._compactions = 0
        self._last_reason = ""

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.BEFORE_CONTEXT, self._on_before_context)
        ctx.register_hook(HookStage.BEFORE_TOOL_CALL, self._allow_compact)

        async def request_compaction() -> ToolResult:
            """Request conversation compaction before the next model call."""
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
            description="Compact conversation history before the next model call.",
            handler=self._compact_command,
            usage="/compact",
            examples=("/compact",),
        ))

    async def _compact_command(self, _ctx: Any, raw_args: str) -> CommandResult:
        if raw_args.strip():
            return CommandResult(
                "Usage: /compact",
                status="error",
                data={"requested": False},
            )
        self._manual_requested = True
        return CommandResult(
            "Conversation compaction requested.",
            data={"requested": True},
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
        threshold_reached = (
            provider_input >= token_trigger
            if provider_input is not None
            else _history_chars(messages) >= self._trigger_chars
        )
        automatic = (
            self._automatic
            and turn != self._last_auto_turn
            and threshold_reached
        )
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
        response = await ctx.invoke_model(
            _summary_request(messages[:split], self._summary_max_chars)
        )
        if response.tool_calls:
            raise RuntimeError("Compaction model must not call tools")
        summary = response.content.strip()
        if not summary:
            raise RuntimeError("Compaction model returned an empty summary")
        summary = summary[:self._summary_max_chars]

        compacted = Message(
            role="system",
            content=f"## Conversation Summary\n\n{summary}",
        )
        self._last_auto_turn = turn
        self._compactions += 1
        self._last_reason = reason
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
        if "input_tokens" in usage:
            value = int(usage.get("input_tokens") or 0)
            return value if value > 0 else None
    return None


def _compact_prefix_end(messages: list[Message], keep_recent_turns: int) -> int:
    user_indexes = [
        index for index, message in enumerate(messages) if message.role == "user"
    ]
    if len(user_indexes) <= keep_recent_turns:
        return 0
    return user_indexes[-keep_recent_turns]


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
