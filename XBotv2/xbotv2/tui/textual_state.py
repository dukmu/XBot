"""Protocol-only helpers shared by Textual TUI tests and runtime."""

from __future__ import annotations

import asyncio

from xbotv2.tui.client import TuiState, _parse_permission_decision

SubmitRoute = str


def route_submitted_text(
    state: TuiState,
    answers: asyncio.Queue[str],
    permission_decisions: asyncio.Queue[str],
    text: str,
) -> SubmitRoute:
    """Route submitted text to a pending live interaction when present."""
    if state.pending_user_input_request_id:
        answers.put_nowait(text)
        return "user_input"
    if state.pending_permission_request_id:
        permission_decisions.put_nowait(_parse_permission_decision(text))
        return "permission"
    return "message"
