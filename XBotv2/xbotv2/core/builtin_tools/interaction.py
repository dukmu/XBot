"""User-interaction tools that emit protocol-visible events."""

from __future__ import annotations

from xbotv2.tools.types import XBotTool


def send_message_to_user(message: str, level: str = "info") -> dict:
    """Send a non-blocking message event to the client user.

    Args:
        message: Message text to display to the user.
        level: UI severity hint such as info, warning, or error.
    """
    return {
        "content": f"Message sent to user: {message}",
        "events": [
            {
                "type": "client_message",
                "data": {
                    "message": message,
                    "level": level,
                },
            }
        ],
    }


def ask_user_for_input(
    question: str,
    options: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    """Ask the client user for input before continuing the current turn.

    Args:
        question: Question text to show to the user.
        options: Optional suggested answers.
        timeout_seconds: Optional timeout. None waits until the client answers
            or disconnects.
    """
    return {
        "content": (
            "User input requested. Waiting for user.input before continuing "
            "the current turn."
        ),
        "wait_for_user": True,
        "timeout_seconds": timeout_seconds,
        "events": [
            {
                "type": "user_input_required",
                "data": {
                    "request_id": "user_input",
                    "source": "ask_user",
                    "question": question,
                    "options": options or [],
                    "timeout_seconds": timeout_seconds,
                },
            }
        ],
    }


send_message = XBotTool.from_function(send_message_to_user, name="send_message")
ask_user = XBotTool.from_function(ask_user_for_input, name="ask_user")

INTERACTION_TOOLS = [send_message, ask_user]
