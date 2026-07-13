"""Conversation history compaction plugin."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    HookContext,
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
        self._keep_recent_turns = 4
        self._summary_max_chars = 8_000
        self._manual_requested = False
        self._last_auto_turn = -1
        self._compactions = 0
        self._last_reason = ""

    async def on_load(self, config: dict[str, Any]) -> None:
        self._automatic = bool(config.get("automatic", True))
        self._trigger_chars = int(config.get("trigger_chars", 80_000))
        self._keep_recent_turns = int(config.get("keep_recent_turns", 4))
        self._summary_max_chars = int(config.get("summary_max_chars", 8_000))

    async def on_unload(self) -> None:
        self._manual_requested = False
        self._last_auto_turn = -1
        self._compactions = 0
        self._last_reason = ""

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.BEFORE_CONTEXT, self._on_before_context)

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

    async def _on_before_context(self, ctx: HookContext):
        messages = list(ctx.state.get("messages") or [])
        manual = self._manual_requested
        turn = ctx.session.turn_count
        automatic = (
            self._automatic
            and turn != self._last_auto_turn
            and _history_chars(messages) >= self._trigger_chars
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
