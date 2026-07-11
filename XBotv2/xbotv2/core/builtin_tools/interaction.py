"""User-interaction tools that emit protocol-visible events."""

from __future__ import annotations

from xbotv2.api.tools import ClientEvent, ToolResult
from xbotv2.api.tools import Tool


def send_message_to_user(message: str, level: str = "info") -> ToolResult:
    """Send a non-blocking message event to the client user.

    Args:
        message: Message text to display to the user.
        level: UI severity hint such as info, warning, or error.
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
    options: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> ToolResult:
    """Ask the client user for input before continuing the current turn.

    Args:
        question: Question text to show to the user.
        options: Optional suggested answers.
        timeout_seconds: Optional timeout. None waits until the client answers
            or disconnects.
    """
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
                "options": options or [],
                "timeout_seconds": timeout_seconds,
            },
        ),),
    )


send_message = Tool.from_function(send_message_to_user, name="send_message")
ask_user = Tool.from_function(ask_user_for_input, name="ask_user")

INTERACTION_TOOLS = [send_message, ask_user]
