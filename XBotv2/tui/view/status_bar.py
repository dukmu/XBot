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

import regex
from rich.text import Text
from rich.cells import cell_len
from textual.widgets import Static

from XBotv2.core.domain import ProviderMeasured, UsageSnapshot
from XBotv2.tui.state import SUBAGENT_THREAD_KIND, SessionState
from XBotv2.tui.status import (
    ServerTurn,
    Status,
    StatusFacts,
    derive,
    status_label,
)

#: Job states that mean "still running", as the server spells them.
_RUNNING_JOB_STATUSES = frozenset({"queued", "running"})

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
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    context_input_tokens: int = 0
    context_input_estimated: bool = False
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
    context_input_tokens, context_input_estimated = _context_usage(state.usage)
    return StatusLine(
        facts=state.facts,
        session_label=session_label or state.title or state.session_id,
        agent_name=state.agent_name,
        provider=state.provider,
        model=state.model,
        model_mode=state.model_mode,
        status_slots=dict(state.status_slots),
        context_window=state.context_window,
        usage=state.usage,
        context_input_tokens=context_input_tokens,
        context_input_estimated=context_input_estimated,
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
    if state.agent_name:
        lines.append(f"  Agent: {state.agent_name}")
    lines += [
        "Runtime",
        f"  State: {status}",
        f"  History: {history}",
        f"  Queued: {len(state.queue)}",
    ]
    if state.jobs:
        running = sum(
            1 for job in state.jobs.values() if job.state in _RUNNING_JOB_STATUSES
        )
        lines.append(f"  Tasks: {running} running of {len(state.jobs)}")
    if state.pending_interactions:
        lines.append(f"  Prompts: {len(state.pending_interactions)} waiting")
    provider = state.provider
    model = state.model
    mode = state.model_mode
    lines.append("Model")
    lines.append(f"  Provider: {provider or '(unknown)'}")
    lines.append(f"  Model: {model or '(unknown)'}" + (f"  ({mode})" if mode else ""))
    context_window = state.context_window
    context_input_tokens, estimated = _context_usage(state.usage)
    if context_window and context_input_tokens:
        marker = "~" if estimated else ""
        if context_input_tokens > context_window:
            over = context_input_tokens - context_window
            lines.append(
                f"  Context: {marker}{context_input_tokens}/{context_window} tokens, "
                f"{over} over"
            )
        else:
            free = round(
                100 * (context_window - context_input_tokens) / context_window
            )
            lines.append(f"  Context: {context_window} tokens, {marker}{free}% free")
    return "\n".join(lines)


def render_status_line(model: StatusLine, *, width: int) -> Text:
    """Render one row of at most ``width`` columns."""
    width = max(1, width)
    status = derive(model.facts)
    label = status_label(status, model.facts)
    segments: list[tuple[str, str]] = [
        (_clip(label, width), _BADGE_STYLE.get(status, "white"))
    ]
    used = cell_len(segments[0][0])
    for text, style in _optional_segments(model):
        if used + 2 + cell_len(text) > width:
            break
        segments.append((text, style))
        used += 2 + cell_len(text)
    rendered = Text()
    for text, style in segments:
        if rendered.plain:
            rendered.append("  ", style="dim")
        rendered.append(text, style=style)
    return rendered


def _optional_segments(model: StatusLine) -> Iterator[tuple[str, str]]:
    """Detail in the order it is given up when the row runs out of room."""
    if model.activity:
        yield model.activity, "dim"
    if model.thread_kind == SUBAGENT_THREAD_KIND:
        # Which thread is on screen is not optional detail: the transcript, the
        # composer and the status all belong to it.
        yield f"subagent:{model.thread_id}", "magenta"
    if model.queue_depth:
        yield f"queued:{model.queue_depth}", "yellow"
    yield from _usage_segments(model)
    context = _context_segment(model)
    if context is not None:
        yield context
    if model.session_label:
        yield f"session:{model.session_label}", "bold"
    if model.provider or model.model:
        yield "/".join(x for x in (model.provider, model.model) if x), "green"
    if model.agent_name:
        yield f"agent:{model.agent_name}", "blue"
    if model.model_mode:
        yield f"mode:{model.model_mode}", "green"
    for name, value in model.status_slots.items():
        yield f"{name}:{value}", "magenta"
    if model.workspace:
        yield f"cwd:{model.workspace}", "cyan"


def _context_segment(model: StatusLine) -> tuple[str, str] | None:
    if model.context_window > 0 and model.context_input_tokens > 0:
        marker = "~" if model.context_input_estimated else ""
        overflow = model.context_input_tokens > model.context_window
        return (
            f"ctx:{marker}{_compact_count(model.context_input_tokens)}"
            f"/{model.context_window}"
            f"{'!' if overflow else ''}",
            "red" if overflow else "cyan",
        )
    return None


def _usage_segments(model: StatusLine) -> Iterator[tuple[str, str]]:
    counters = model.usage.total_counters
    if counters.input or counters.output:
        yield f"in:{_compact_count(counters.input)}", ""
        yield f"out:{_compact_count(counters.output)}", ""
    if counters.cache_read:
        cache_basis = counters.input + counters.cache_read
        if cache_basis:
            cache_rate = round(100 * counters.cache_read / cache_basis)
            yield f"cache:{cache_rate}%", "green"


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def _context_usage(snapshot: UsageSnapshot) -> tuple[int, bool]:
    observation = snapshot.latest_turn_observation
    if observation is None:
        return 0, False
    if isinstance(observation.observed_context, ProviderMeasured):
        return observation.observed_context.tokens, False
    return observation.estimated_input_tokens, True


def _clip(label: str, width: int) -> str:
    """Shorten to ``width`` columns, marking that something was cut."""
    if width <= 0:
        return ""
    if cell_len(label) <= width:
        return label
    marker = "…"
    available = max(0, width - cell_len(marker))
    result: list[str] = []
    used = 0
    for cluster in regex.findall(r"\X", label):
        cluster_width = cell_len(cluster)
        if used + cluster_width > available:
            break
        result.append(cluster)
        used += cluster_width
    return "".join(result) + marker


class StatusBar(Static):
    """One row showing the derived status and the detail that fits."""

    DEFAULT_CSS = STATUS_BAR_CSS

    def show(self, model: StatusLine, *, width: int | None = None) -> None:
        self.update(render_status_line(model, width=width or self.size.width or 80))


__all__ = [
    "status_report", "STATUS_BAR_CSS", "StatusBar", "StatusLine",
    "render_status_line", "status_line_for"]
