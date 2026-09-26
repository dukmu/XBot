"""Tools owned by the interactions plugin."""

from __future__ import annotations

import inspect
import json
from typing import Literal

from XBotv2.core.parts import TextPart
from XBotv2.core.tools import Tool, ToolCall, ToolError, ToolFailed, ToolOutput, ToolSucceeded, ToolOutcome
from XBotv2.interactions import Answered, InteractionsPort, UserInputOption


_ASK_USER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "question": {"type": "string", "minLength": 1},
        "options": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
                "required": ["label", "description"],
            },
        },
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": ["question", "options"],
}


def send_message_to_user(
    message: str,
    level: Literal["info", "warning", "error"] = "info",
) -> ToolSucceeded:
    """Send a non-blocking progress update to the client.

    Never use this tool for the final answer in the main conversation. Return
    the final answer as the assistant response so it remains the canonical
    reply in the transcript.
    """
    return ToolSucceeded(output=ToolOutput(parts=(TextPart(text=f"Message sent to user: {message}"),)))


async def ask_user_for_input(
    question: str,
    options: tuple[UserInputOption, ...],
    timeout_seconds: float | None = None,
    *,
    interactions: InteractionsPort,
    tool_call_id: str = "",
) -> ToolSucceeded | ToolFailed:
    """Pause this tool call until the client answers one necessary question."""
    result = await interactions.request_user_input(
        question,
        options=options,
        source="ask_user",
        timeout_seconds=timeout_seconds,
        tool_call_id=tool_call_id,
    )
    if not isinstance(result, Answered):
        return ToolFailed(
            error=ToolError(
                code="interaction_not_answered",
                message=f"User input was not answered ({result.kind}).",
            ),
            output=ToolOutput(),
        )
    answer = result.answer
    content = (
        answer
        if isinstance(answer, str)
        else json.dumps(answer, ensure_ascii=False, default=str)
    )
    return ToolSucceeded(output=ToolOutput(parts=(TextPart(text=content),)))


def build_ask_user_tool(interactions: InteractionsPort) -> Tool:
    """Bind one session's interaction service to its Agent-facing Tool."""

    async def invoke(
        question: str,
        options: list[dict[str, str]],
        timeout_seconds: float | None = None,
        *,
        tool_call: ToolCall,
    ) -> ToolSucceeded | ToolFailed:
        return await ask_user_for_input(
            question,
            tuple(UserInputOption.model_validate(option) for option in options),
            timeout_seconds,
            interactions=interactions,
            tool_call_id=tool_call.id,
        )

    return Tool(
        name="ask_user",
        description=inspect.getdoc(ask_user_for_input) or "",
        function=invoke,
        parameters=_ASK_USER_SCHEMA,
        tool_call_parameter="tool_call",
    )


send_message = Tool.from_function(send_message_to_user, name="send_message")
