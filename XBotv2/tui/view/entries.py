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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Static

from XBotv2.tui.view.blocks import BLOCK_PREVIEW_LINES, ClampedBlock
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
    margin-bottom: 0;
}
EntryWidget.user {
    background: $panel;
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
_NOTICE_HEADERS = {
    "interaction:permission": "? Permission required",
    "interaction:user_input": "? Question",
}

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
        return _tool_call_label(entry)
    if isinstance(entry, NoticeEntry):
        return _NOTICE_HEADERS.get(entry.notice_kind, entry.notice_kind)
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
        return "Output"
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
    if isinstance(entry, (AssistantEntry, UserEntry, ErrorEntry)):
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
    markdown = isinstance(entry, AssistantEntry) and looks_like_markdown(body)
    if isinstance(entry, UserEntry):
        note = _DELIVERY_NOTES[entry.delivery]
        body = _marked_text("❯", body, note=note)
    elif isinstance(entry, AssistantEntry):
        body = _marked_text("●", body)
    if markdown:
        return Markdown(body, code_theme="monokai")
    return Text(body)


def _marked_text(marker: str, body: str, *, note: str = "") -> str:
    """Render a conversation turn as content, not as a labelled log record."""
    lines = body.splitlines() or [""]
    first = f"{marker} {lines[0]}"
    if note:
        first = f"{first}  ({note})"
    return "\n".join((first, *(f"  {line}" for line in lines[1:])))


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

    Reasoning and tool details are clamped blocks. The assistant's answer is the
    conversation itself, so it remains ordinary transcript content regardless
    of length; folding it would make the final response look like diagnostics.
    """
    children: list[Widget] = []
    if isinstance(entry, ToolEntry):
        children.extend(_tool_blocks(entry, visibility=visibility))
        return EntryWidget(*children, name=entry.id, classes=entry_classes(entry))
    if not isinstance(entry, (UserEntry, AssistantEntry)):
        children.append(Static(_header(entry, assistant_label), classes="meta"))
    reasoning = entry_reasoning(entry, visibility=visibility)
    if reasoning is not None:
        children.append(ClampedBlock(
            str(reasoning.plain),
            label="Think",
            classes="reasoning",
            always_show_label=True,
            always_collapsible=True,
            collapse_after_streaming=True,
            streaming=isinstance(entry, AssistantEntry) and entry.streaming,
        ))
    body = entry_body(entry, visibility=visibility)
    if body:
        children.append(_body_block(entry, body))
    return EntryWidget(*children, name=entry.id, classes=entry_classes(entry))


def _body_block(entry: Entry, body: str) -> Widget:
    if not clamped(entry):
        return Static(entry_body_renderable(entry, body=body), classes="body")
    preview_lines = 0 if isinstance(entry, ToolEntry) else BLOCK_PREVIEW_LINES
    return ClampedBlock(
        body,
        label=block_label(entry),
        renderable=entry_body_renderable(entry, body=body),
        classes="body",
        always_collapsible=isinstance(entry, ToolEntry),
        streaming=isinstance(entry, AssistantEntry) and entry.streaming,
        preview_lines=preview_lines,
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
    if not widget.is_mounted:
        return False
    meta = _child(widget, ".meta")
    if meta is not None:
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
        str(reasoning.plain),
        label="Think",
        classes="reasoning",
        always_show_label=True,
        always_collapsible=True,
        collapse_after_streaming=True,
        streaming=streaming,
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
    if isinstance(entry, ToolEntry):
        await _sync_tool_blocks(widget, entry, visibility=visibility)
        return
    existing = _child(widget, ".body")
    body = entry_body(entry, visibility=visibility)
    if not body:
        if existing is not None:
            await existing.remove()
        return
    wants_clamped = clamped(entry)
    if isinstance(existing, ClampedBlock) and wants_clamped:
        existing.show(
            body, renderable=entry_body_renderable(entry, body=body), streaming=streaming
        )
        return
    if isinstance(existing, Static) and not wants_clamped:
        existing.update(entry_body_renderable(entry, body=body))
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


def _tool_call_label(entry: ToolEntry) -> str:
    call = entry.name
    arguments = _tool_arguments(entry.args)
    if arguments:
        call = f"{call}({arguments})"
    elif entry.status in _UNFINISHED_TOOL_STATUSES:
        call = f"{call}(*)"
    return f"● {call}"


def _tool_outcome_label(entry: ToolEntry) -> str:
    if entry.status in _UNFINISHED_TOOL_STATUSES:
        return f"  ⎿ {entry.status.title()}…"
    elapsed = max(0.0, entry.finished_at - entry.started_at)
    outcome = "Done" if entry.status == "success" else entry.status.title()
    return f"  ⎿ {outcome} · {elapsed:.1f}s"


def _tool_arguments(args: Mapping[str, Any]) -> str:
    """One bounded call summary; the complete payload remains in details."""
    if not args:
        return ""
    summary = ", ".join(f"{key}: {format_payload(value)}" for key, value in args.items())
    summary = " ".join(summary.split())
    return summary if len(summary) <= 34 else f"{summary[:31]}…"


def _tool_blocks(
    entry: ToolEntry, *, visibility: BlockVisibility | None
) -> tuple[ClampedBlock, ClampedBlock]:
    details = visibility is None or visibility.details
    args = format_payload(entry.args) if details and entry.args else ""
    result = format_payload(entry.result) if details and entry.result not in ("", None) else ""
    return (
        ClampedBlock(
            args,
            label=_tool_call_label(entry),
            classes="tool-call",
            always_show_label=True,
            always_collapsible=bool(args),
            preview_lines=0,
            inline_label=True,
        ),
        ClampedBlock(
            result,
            label=_tool_outcome_label(entry),
            classes="tool-result",
            always_show_label=True,
            always_collapsible=bool(result),
            preview_lines=0,
            inline_label=True,
        ),
    )


async def _sync_tool_blocks(
    widget: EntryWidget,
    entry: ToolEntry,
    *,
    visibility: BlockVisibility | None,
) -> None:
    planned = _tool_blocks(entry, visibility=visibility)
    details = visibility is None or visibility.details
    texts = (
        format_payload(entry.args) if details and entry.args else "",
        format_payload(entry.result)
        if details and entry.result not in ("", None)
        else "",
    )
    for selector, replacement, text in zip(
        (".tool-call", ".tool-result"), planned, texts
    ):
        existing = _child(widget, selector)
        if isinstance(existing, ClampedBlock):
            existing.label = replacement.label
            existing.always_show_label = replacement.always_show_label
            existing.always_collapsible = replacement.always_collapsible
            existing.inline_label = replacement.inline_label
            existing.show(text)
        elif existing is None:
            await widget.mount(replacement)
        else:  # pragma: no cover - the classes are owned by this module
            await existing.remove()
            await widget.mount(replacement)


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
