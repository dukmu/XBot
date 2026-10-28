"""Session and thread chooser rows."""

from __future__ import annotations

from typing import Sequence
from XBotv2.session.api.contracts import SessionSummary, ThreadSummary
from XBotv2.tui.impl.view.selection import Option


def filter_options(options: Sequence[Option], query: str) -> tuple[Option, ...]:
    """Rows matching every term of ``query``, in the order they were built.

    The chooser offers a filter box for every list it shows, so a catalogue with
    dozens of models stays usable with the same three keystrokes it takes to
    narrow the palette.
    """
    terms = query.strip().lower().split()
    if not terms:
        return tuple(options)
    kept = []
    for option in options:
        haystack = f"{option.label} {option.detail}".lower()
        if all(term in haystack for term in terms):
            kept.append(option)
    return tuple(kept)


def _detail(*parts: str) -> str:
    return " ".join(str(part) for part in parts if part)


def session_options(
    items: Sequence[SessionSummary], *, current: str = ""
) -> tuple[Option, ...]:
    """Rows for ``/session``: one per stored session."""
    return tuple(
        Option(
            value=item.session_id,
            label=(
                f"{item.session_id} · {item.title}"
                if item.title and item.title != item.session_id
                else item.session_id
            ),
            detail=_detail(
                "current" if item.session_id == current else "",
                "damaged" if item.unreadable else item.status,
                "empty" if item.blank else "",
                (
                    f"{item.thread_count} threads"
                    if item.thread_count > 1
                    else ""
                ),
                item.workspace_root,
            ),
        )
        for item in items
    )


def thread_options(
    items: Sequence[ThreadSummary], *, current: str = ""
) -> tuple[Option, ...]:
    """Rows for ``/thread`` with identity and server-owned liveness."""
    return tuple(
        Option(
            value=item.thread_id,
            label=(
                (
                    f"Main · {item.title} · {item.thread_id}"
                    if item.title and item.title != item.thread_id
                    else f"Main · {item.thread_id}"
                )
                if item.kind == "main"
                else (
                    f"{item.title} · {item.thread_id}"
                    if item.title and item.title != item.thread_id
                    else f"{item.agent or 'subagent'} · {item.thread_id}"
                )
            ),
            detail=_detail(
                "current" if item.thread_id == current else "",
                "subagent" if item.kind == "subagent" else "",
                (
                    "closed"
                    if item.status == "inactive"
                    else "working" if item.turn_status == "running" else "idle"
                ),
                item.agent if item.kind == "subagent" else "",
                f"{item.message_count} msg" if item.message_count else "",
            ),
        )
        for item in items
    )


__all__ = ["filter_options", "session_options", "thread_options"]
