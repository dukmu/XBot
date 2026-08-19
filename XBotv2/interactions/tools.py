"""Tools owned by the interactions plugin."""

from __future__ import annotations

import inspect
import json
from typing import Any, Literal

from XBotv2.core.tools import ClientEvent, Tool, ToolResult


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
) -> ToolResult:
    """Send a non-blocking progress or diagnostic message to the client."""
    return ToolResult(
        content=f"Message sent to user: {message}",
        client_events=(ClientEvent(
            type="client_message",
            data={"message": message, "level": level, "source": "send_message"},
        ),),
    )


async def ask_user_for_input(
    question: str,
    options: list[dict[str, str]],
    timeout_seconds: float | None = None,
    *,
    interactions: Any = None,
    tool_call_id: str = "",
) -> ToolResult:
    """Pause this tool call until the client answers one necessary question."""
    if interactions is None:
        return ToolResult.failure(
            "interaction_unavailable",
            "User input is unavailable in this session.",
        )
    result = await interactions.request_user_input(
        question,
        options=options,
        source="ask_user",
        timeout_seconds=timeout_seconds,
        tool_call_id=tool_call_id,
    )
    if result.get("status") != "answered":
        status = str(result.get("status") or "unavailable")
        return ToolResult.failure(
            "interaction_not_answered",
            f"User input was not answered ({status}).",
        )
    answer = result.get("answer", "")
    content = (
        answer
        if isinstance(answer, str)
        else json.dumps(answer, ensure_ascii=False, default=str)
    )
    return ToolResult.success(content)


send_message = Tool.from_function(send_message_to_user, name="send_message")
ask_user = Tool(
    name="ask_user",
    description=inspect.getdoc(ask_user_for_input) or "",
    function=ask_user_for_input,
    parameters=_ASK_USER_SCHEMA,
    injected_parameters=("interactions", "tool_call_id"),
)
