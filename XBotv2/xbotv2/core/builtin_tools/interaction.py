"""User-interaction tools that emit protocol-visible events."""

from __future__ import annotations

from langchain_core.tools import tool as langchain_tool


@langchain_tool
def send_message(message: str, level: str = "info") -> dict:
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


@langchain_tool
def ask_user(question: str, options: list[str] | None = None) -> dict:
    """Ask the client user for input and stop the current turn.

    Args:
        question: Question text to show to the user.
        options: Optional suggested answers.
    """
    return {
        "content": "User input requested. Resume is not implemented yet.",
        "status": "error",
        "turn_complete": True,
        "events": [
            {
                "type": "user_input_required",
                "data": {
                    "request_id": "user_input",
                    "source": "ask_user",
                    "question": question,
                    "options": options or [],
                    "resume_supported": False,
                },
            }
        ],
    }


INTERACTION_TOOLS = [send_message, ask_user]
