"""User-interaction tools that emit protocol-visible events."""

from __future__ import annotations

import inspect
from typing import Literal

from xbotv2.api.tools import ClientEvent, ToolResult
from xbotv2.api.tools import Tool


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
    """Send a non-blocking progress or diagnostic message to the client.

    Use this only when the human should see information before the final assistant
    response. It does not ask a question, pause the turn, or replace the final
    response.

    Args:
        message: Concise text to display to the human.
        level: Severity hint: info for progress, warning for recoverable issues,
            or error for a failure the human must know about.
    """
    return ToolResult(
        content=f"Message sent to user: {message}",
        client_events=(ClientEvent(
            type="client_message",
            data={"message": message, "level": level},
        ),),
    )


def ask_user_for_input(
    question: str,
    options: list[dict[str, str]],
    timeout_seconds: float | None = None,
) -> ToolResult:
    """Pause the current turn until the client answers one necessary question.

    Use this only when work cannot safely continue without human information or
    a decision. Do not ask for confirmation when the user already delegated the
    choice. Each suggested option needs a short label and a description that
    explains its impact. The selected label is returned as this tool's result,
    after which the agent continues the same turn.

    Args:
        question: One direct question shown to the human.
        options: Two or more label and description objects shown as choices.
        timeout_seconds: Optional positive wait limit in seconds. None waits until
            the client answers, disconnects, or interrupts the turn.
    """
    if len(options) < 2 or any(
        not isinstance(option, dict)
        or not isinstance(option.get("label"), str)
        or not option["label"].strip()
        or not isinstance(option.get("description"), str)
        or not option["description"].strip()
        for option in options
    ):
        raise ValueError(
            "options must contain at least two non-empty label/description choices"
        )

    return ToolResult(
        content=(
            "User input requested. Waiting for user.input before continuing "
            "the current turn."
        ),
        wait_for_user=True,
        timeout_seconds=timeout_seconds,
        client_events=(ClientEvent(
            type="user_input_required",
            data={
                "request_id": "user_input",
                "source": "ask_user",
                "question": question,
                "options": options,
                "timeout_seconds": timeout_seconds,
            },
        ),),
    )


send_message = Tool.from_function(send_message_to_user, name="send_message")
ask_user = Tool(
    name="ask_user",
    description=inspect.getdoc(ask_user_for_input) or "",
    function=ask_user_for_input,
    parameters=_ASK_USER_SCHEMA,
)

INTERACTION_TOOLS = [send_message, ask_user]
