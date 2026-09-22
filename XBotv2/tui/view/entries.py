"""Timeline entries become widgets, and update in place.

Two rules, both about not doing work that hides a bug:

* the *formatting* is pure -- header and body text are functions of the entry, so
  they are tested without a terminal;
* an entry's widget is created from that entry and later updated with that same
  entry. Nothing else may write into it, which is what stops a streamed answer
  from landing in a different message's widget.

Whether an update is needed is the caller's decision: it holds the entry it last
rendered for each id and compares. That keeps this module free of "did anything
change?" guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Static

from XBotv2.tui.view.blocks import ClampedBlock
from XBotv2.tui.timeline import (
    AssistantEntry,
    Delivery,
    Entry,
    ErrorEntry,
    NoticeEntry,
    ToolEntry,
    UserEntry,
)

ENTRY_CSS = """
EntryWidget {
    height: auto;
    width: 1fr;
}
EntryWidget > .meta {
    height: auto;
    text-style: bold;
}
EntryWidget > .body, EntryWidget > .reasoning {
    height: auto;
    width: 1fr;
}
EntryWidget ClampedBlock:focus {
    border-left: thick $accent;
}
"""


@dataclass(frozen=True)
class BlockVisibility:
    """Which optional blocks the reader asked to see.

    A rendering preference, not session state: it belongs to the person at the
    terminal, not to the conversation, so nothing about it travels to the server
    or reaches the reducer.
    """

    reasoning: bool = True
    details: bool = True


def block_choice(argument: str, *, current: bool) -> bool | None:
    """The new value for one block, or ``None`` when the argument is not one.

    ``None`` is what lets the caller say "usage" instead of guessing, so a typo
    cannot silently flip a display setting.
    """
    value = argument.strip().lower()
    if value in {"", "toggle"}:
        return not current
    if value == "on":
        return True
    if value == "off":
        return False
    return None


class EntryWidget(Vertical):
    """One timeline entry.

    The styles matter: without an explicit ``height: auto`` the container's
    default fills the viewport, so a transcript of several entries cannot
    scroll and every scroll assertion becomes meaningless.
    """

    DEFAULT_CSS = ENTRY_CSS


_DELIVERY_NOTES: dict[Delivery, str] = {
    Delivery.PENDING: "sending…",
    Delivery.ACCEPTED: "",
    Delivery.FAILED: "not delivered",
}

_UNFINISHED_TOOL_STATUSES = frozenset({"pending", "running"})

#: Notices the user explicitly asked for: reference output, not chatter.
_REQUESTED_REPORTS = frozenset({"help", "status", "jobs"})

_MARKDOWN_INLINE_MARKERS: tuple[str, ...] = ("```", "##", "**")
_MARKDOWN_LINE_PREFIXES: tuple[str, ...] = ("- ", "* ", "> ", "|")
_ORDERED_ITEM = re.compile(r"^\d+[.)] ")


def entry_header(entry: Entry, *, assistant_label: str = "Assistant") -> str:
    """The one-line summary above an entry's body."""
    if isinstance(entry, UserEntry):
        note = _DELIVERY_NOTES[entry.delivery]
        return f"You  ({note})" if note else "You"
    if isinstance(entry, AssistantEntry):
        return f"{assistant_label}  replying…" if entry.streaming else assistant_label
    if isinstance(entry, ToolEntry):
        return _tool_header(entry)
    if isinstance(entry, NoticeEntry):
        return entry.notice_kind
    if isinstance(entry, ErrorEntry):
        return "error"
    raise TypeError(f"Unsupported entry: {entry!r}")


def entry_body(entry: Entry, *, visibility: BlockVisibility | None = None) -> str:
    """The entry's body text, with structured payloads rendered readably."""
    if isinstance(entry, (UserEntry, AssistantEntry)):
        return entry.content
    if isinstance(entry, ToolEntry):
        if visibility is not None and not visibility.details:
            # The call itself stays visible in the header; only the payload goes.
            return ""
        parts: list[str] = []
        if entry.args:
            parts.append(f"args: {format_payload(entry.args)}")
        if entry.result not in ("", None):
            parts.append(f"result: {format_payload(entry.result)}")
        return "\n".join(parts)
    if isinstance(entry, NoticeEntry):
        return f"{entry.text}\n{entry.detail}".strip() if entry.detail else entry.text
    if isinstance(entry, ErrorEntry):
        return entry.message
    raise TypeError(f"Unsupported entry: {entry!r}")


def block_label(entry: Entry) -> str:
    """What the summary row of this entry's block calls the content."""
    if isinstance(entry, ToolEntry):
        return "tool output"
    if isinstance(entry, AssistantEntry):
        return "reply"
    if isinstance(entry, UserEntry):
        return "message"
    if isinstance(entry, NoticeEntry):
        return entry.notice_kind
    return "output"


def clamped(entry: Entry) -> bool:
    """Whether this entry's content may be folded away.

    Two kinds never are. An error, because I4 says a failure has to be visible
    and a folded message would hide the one thing the reader needs. And the help
    listing, because it is reference output the user explicitly asked for: a
    catalogue that says "… 12 more lines" is a command that does not work.
    """
    if isinstance(entry, ErrorEntry):
        return False
    if isinstance(entry, NoticeEntry) and entry.notice_kind in _REQUESTED_REPORTS:
        return False
    return True


def entry_classes(entry: Entry) -> str:
    return f"entry {entry.kind.value}"


def entry_reasoning(
    entry: Entry, *, visibility: BlockVisibility | None = None
) -> Text | None:
    """Model reasoning, rendered distinctly from the answer."""
    if visibility is not None and not visibility.reasoning:
        return None
    if isinstance(entry, AssistantEntry) and entry.reasoning:
        return Text(entry.reasoning, style="dim italic")
    return None


def entry_body_renderable(entry: Entry, *, body: str | None = None) -> Text | Markdown:
    """The body, parsed as Markdown only when there is Markdown in it."""
    body = entry_body(entry) if body is None else body
    if isinstance(entry, AssistantEntry) and looks_like_markdown(body):
        return Markdown(body, code_theme="monokai")
    return Text(body)


def format_payload(value: JsonValue) -> str:
    """Render a structured payload readably, without decoding it twice."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def entry_widget(
    entry: Entry,
    *,
    assistant_label: str = "Assistant",
    visibility: BlockVisibility | None = None,
) -> EntryWidget:
    """Build the widget for one entry.

    The body and the reasoning are *clamped* blocks: a 500-line tool result or a
    long answer folds into a few lines with a summary row, so one verbose step
    cannot push the conversation off the screen.
    """
    children: list[Widget] = [Static(_header(entry, assistant_label), classes="meta")]
    reasoning = entry_reasoning(entry, visibility=visibility)
    if reasoning is not None:
        children.append(ClampedBlock(
            str(reasoning.plain),
            label="thinking",
            classes="reasoning",
            streaming=isinstance(entry, AssistantEntry) and entry.streaming,
        ))
    body = entry_body(entry, visibility=visibility)
    if body:
        children.append(_body_block(entry, body))
    return EntryWidget(*children, classes=entry_classes(entry))


def _body_block(entry: Entry, body: str) -> Widget:
    if not clamped(entry):
        return Static(entry_body_renderable(entry, body=body), classes="body")
    return ClampedBlock(
        body,
        label=block_label(entry),
        renderable=entry_body_renderable(entry, body=body),
        classes="body",
        streaming=isinstance(entry, AssistantEntry) and entry.streaming,
    )


async def update_entry_widget(
    widget: EntryWidget,
    entry: Entry,
    *,
    assistant_label: str = "Assistant",
    visibility: BlockVisibility | None = None,
) -> bool:
    """Refresh a widget in place from its own entry.

    Returns False when the widget has not composed its children yet, which
    happens when an entry changes in the same frame it was mounted. The caller
    must then *not* record the entry as rendered: the update would otherwise be
    lost, and a stale header (say "sending…") would stay on screen forever.
    """
    meta = _child(widget, ".meta")
    if meta is None:
        return False
    meta.update(_header(entry, assistant_label))
    streaming = isinstance(entry, AssistantEntry) and entry.streaming
    await _sync_reasoning(widget, entry, visibility=visibility, streaming=streaming)
    await _sync_body(widget, entry, visibility=visibility, streaming=streaming)
    return True


async def _sync_reasoning(
    widget: EntryWidget,
    entry: Entry,
    *,
    visibility: BlockVisibility | None,
    streaming: bool,
) -> None:
    existing = _child(widget, ".reasoning")
    reasoning = entry_reasoning(entry, visibility=visibility)
    if reasoning is None:
        if existing is not None:
            await existing.remove()
        return
    if isinstance(existing, ClampedBlock):
        existing.show(str(reasoning.plain), streaming=streaming)
        return
    before = widget.children[0] if widget.children else None
    block = ClampedBlock(
        str(reasoning.plain), label="thinking", classes="reasoning", streaming=streaming
    )
    if before is not None:
        await widget.mount(block, before=before)
    else:
        await widget.mount(block)


async def _sync_body(
    widget: EntryWidget,
    entry: Entry,
    *,
    visibility: BlockVisibility | None,
    streaming: bool,
) -> None:
    """Bring the body in line with the entry, updating the block in place.

    A streamed answer grows every frame, so this must never rebuild the block:
    the reader would lose their place in it (and the scroll position inside a
    clamped block) on every delta.
    """
    existing = _child(widget, ".body")
    body = entry_body(entry, visibility=visibility)
    if not body:
        if existing is not None:
            await existing.remove()
        return
    if isinstance(existing, ClampedBlock):
        existing.show(
            body, renderable=entry_body_renderable(entry, body=body), streaming=streaming
        )
        return
    if existing is not None:
        await existing.remove()
    await widget.mount(_body_block(entry, body))


def looks_like_markdown(text: str) -> bool:
    """Cheap structural test; plain prose is rendered as plain text.

    The point is not to be a Markdown parser: it is to avoid turning an ordinary
    sentence into a reflowed document.
    """
    if any(marker in text for marker in _MARKDOWN_INLINE_MARKERS):
        return True
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_MARKDOWN_LINE_PREFIXES) or _ORDERED_ITEM.match(stripped):
            return True
    return False


def _header(entry: Entry, assistant_label: str) -> Text:
    return Text(entry_header(entry, assistant_label=assistant_label))


def _tool_header(entry: ToolEntry) -> str:
    if entry.status in _UNFINISHED_TOOL_STATUSES:
        return f"tool {entry.name}  {entry.status}…"
    elapsed = max(0.0, entry.finished_at - entry.started_at)
    return f"tool {entry.name}  {entry.status}  {elapsed:.1f}s"


def _child(widget: EntryWidget, selector: str) -> Widget | None:
    """A child that may not have composed yet.

    Textual composes children on the next cycle, so an update that races the
    first mount legitimately finds nothing; the caller re-renders afterwards.
    """
    try:
        return widget.query(selector).first()
    except NoMatches:
        return None


__all__ = [
    "ENTRY_CSS",
    "EntryWidget",
    "entry_body",
    "entry_body_renderable",
    "entry_classes",
    "entry_header",
    "entry_reasoning",
    "entry_widget",
    "format_payload",
    "looks_like_markdown",
    "update_entry_widget",
]
