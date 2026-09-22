"""The status line: one row, derived from facts, never wider than the terminal.

Rendering is a pure function of a :class:`StatusLine` model, so the line can be
checked without a terminal. Two rules:

* the status word comes from :func:`status.derive`; this module never decides
  whether a turn is running;
* the line never exceeds the width it is given, and detail yields in priority
  order so the status word survives a narrow terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Mapping

from rich.text import Text
from textual.widgets import Static

from XBotv2.core.usage import UsageData
from XBotv2.tui.state import SUBAGENT_THREAD_KIND, SessionState
from XBotv2.tui.status import (
    ServerTurn,
    Status,
    StatusFacts,
    derive,
    status_label,
)

#: Job states that mean "still running", as the server spells them.
_RUNNING_JOB_STATUSES = frozenset({"pending", "running"})

_BADGE_STYLE: dict[Status, str] = {
    Status.CONNECTING: "yellow",
    Status.DISCONNECTED: "red",
    Status.READY: "green",
    Status.RUNNING: "yellow",
    Status.WAITING_USER: "cyan",
    Status.APPROVAL_REQUIRED: "magenta",
    Status.COMPACTING: "blue",
    Status.INTERRUPTING: "yellow",
    Status.ERROR: "red",
}

STATUS_BAR_CSS = """
StatusBar {
    height: 1;
    width: 1fr;
    padding: 0 1;
}
"""


@dataclass(frozen=True)
class StatusLine:
    """Everything the status line may show.

    ``facts`` is the only source of the status word; the rest is detail that is
    dropped, in order, as the terminal narrows.
    """

    facts: StatusFacts
    session_label: str = ""
    agent_name: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    status_slots: Mapping[str, str] = field(default_factory=dict, hash=False)
    context_window: int = 0
    usage: UsageData = field(default_factory=UsageData)
    context_input_tokens: int = 0
    queue_depth: int = 0
    activity: str = ""
    workspace: str = ""
    thread_id: str = ""
    thread_kind: str = ""


def status_line_for(
    state: SessionState,
    *,
    session_label: str = "",
    workspace: str = "",
    activity: str = "",
) -> StatusLine:
    """Build the model from the session state; the view adds no facts of its own."""
    return StatusLine(
        facts=state.facts,
        session_label=session_label or state.title or state.session_id,
        agent_name=state.agent_name,
        provider=state.provider,
        model=state.model,
        model_mode=state.model_mode,
        status_slots=dict(state.status_slots),
        context_window=state.context_window,
        usage=UsageData(**{key: state.usage.get(key, 0) for key in UsageData.model_fields}),
        context_input_tokens=state.context_input_tokens,
        queue_depth=len(state.queue),
        activity=activity,
        workspace=workspace,
        thread_id=state.thread_id,
        thread_kind=state.thread_kind,
    )


def status_report(
    state: SessionState,
    *,
    workspace: str = "",
    activity: str = "",
) -> str:
    """The multi-line report ``/status`` shows, rendered from local state.

    Everything here is already on the client: the thread read, the derived
    status, the queue, the jobs, the usage. The server has a ``/status`` command
    that assembles similar text for clients that have none of this; the TUI does
    not need the round trip, and it can add what the server cannot know -- how
    long the running turn has been going.
    """
    thread = state.thread
    if thread is None:
        return (
            "No thread read yet: the client has not had an authoritative answer "
            "from the server."
        )
    status = status_label(derive(state.facts), state.facts)
    if activity:
        status = f"{status}  {activity}"
    history = f"{thread.message_count} messages"
    turns = int(thread.session_stats.turns or 0)
    if turns:
        history = f"{history}, {turns} turns"

    lines = ["Session"]
    if thread.title:
        lines.append(f"  Title: {thread.title}")
    lines.append(f"  ID: {thread.session_id or state.session_id}")
    thread_line = f"  Thread: {thread.thread_id or state.thread_id}"
    if state.read_only:
        thread_line = f"{thread_line}  ({thread.kind}, read-only)"
    lines.append(thread_line)
    root = thread.workspace_root or workspace
    if root:
        lines.append(f"  Workspace: {root}")
    if thread.agent:
        lines.append(f"  Agent: {thread.agent}")
    lines += [
        "Runtime",
        f"  State: {status}",
        f"  History: {history}",
        f"  Queued: {len(state.queue)}",
    ]
    if state.jobs:
        running = sum(
            1 for job in state.jobs.values() if str(job.status) in _RUNNING_JOB_STATUSES
        )
        lines.append(f"  Tasks: {running} running of {len(state.jobs)}")
    if state.pending_interactions:
        lines.append(f"  Prompts: {len(state.pending_interactions)} waiting")
    provider = thread.provider or state.provider
    model = thread.model or state.model
    mode = thread.model_mode or state.model_mode
    lines.append("Model")
    lines.append(f"  Provider: {provider or '(unknown)'}")
    lines.append(f"  Model: {model or '(unknown)'}" + (f"  ({mode})" if mode else ""))
    context_window = thread.context_window or state.context_window
    if context_window:
        free = round(
            100 * max(0, context_window - state.context_input_tokens) / context_window
        )
        lines.append(f"  Context: {context_window} tokens, {free}% free")
    return "\n".join(lines)


def render_status_line(model: StatusLine, *, width: int) -> Text:
    """Render one row of at most ``width`` columns."""
    width = max(1, width)
    status = derive(model.facts)
    label = status_label(status, model.facts)
    segments: list[tuple[str, str]] = [
        (_clip(label, width), _BADGE_STYLE.get(status, "white"))
    ]
    used = len(segments[0][0])
    for text, style in _optional_segments(model):
        if used + 2 + len(text) > width:
            continue
        segments.append((text, style))
        used += 2 + len(text)
    rendered = Text()
    for text, style in segments:
        if rendered.plain:
            rendered.append("  ", style="dim")
        rendered.append(text, style=style)
    return rendered


def _optional_segments(model: StatusLine) -> Iterator[tuple[str, str]]:
    """Detail in the order it is given up when the row runs out of room."""
    if model.thread_kind == SUBAGENT_THREAD_KIND:
        # Which thread is on screen is not optional detail: the transcript, the
        # composer and the status all belong to it.
        yield f"subagent:{model.thread_id}", "magenta"
    if model.queue_depth:
        yield f"queued:{model.queue_depth}", "yellow"
    if model.activity:
        yield model.activity, "dim"
    total = int(model.usage.total_tokens or 0)
    if total:
        yield f"tokens:{_compact_count(total)}", ""
    if model.context_window > 0 and model.context_input_tokens > 0:
        free = round(
            100 * max(0, model.context_window - model.context_input_tokens)
            / model.context_window
        )
        yield f"ctx-free:{free}%", "cyan"
    if model.session_label:
        yield f"session:{model.session_label}", "dim"
    if model.agent_name:
        yield f"agent:{model.agent_name}", "blue"
    identity = "/".join(part for part in (model.provider, model.model) if part)
    if identity:
        if model.model_mode:
            identity = f"{identity}:{model.model_mode}"
        yield identity, "green"
    for name, value in model.status_slots.items():
        yield f"{name}:{value}", "magenta"
    if model.workspace:
        yield f"cwd:{model.workspace}", "cyan"


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def _clip(label: str, width: int) -> str:
    """Shorten to ``width`` columns, marking that something was cut."""
    if width <= 0:
        return ""
    if len(label) <= width:
        return label
    if width <= 3:
        return label[:width]
    return f"{label[: width - 3]}..."


class StatusBar(Static):
    """One row showing the derived status and the detail that fits."""

    DEFAULT_CSS = STATUS_BAR_CSS

    def show(self, model: StatusLine, *, width: int | None = None) -> None:
        self.update(render_status_line(model, width=width or self.size.width or 80))


__all__ = [
    "status_report","STATUS_BAR_CSS", "StatusBar", "StatusLine", "render_status_line", "status_line_for"]
