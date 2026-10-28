"""Compact terminal context derived from session facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Mapping
import regex
from rich.text import Text
from rich.cells import cell_len
from textual.widgets import Static
from XBotv2.core.api.domain import ProviderMeasured, UsageSnapshot
from XBotv2.tui.impl.state import SUBAGENT_THREAD_KIND, SessionState
from XBotv2.tui.impl.status import Status, StatusFacts, derive, status_label

_BADGE_STYLE: dict[Status, str] = {
    Status.CONNECTING: "dim",
    Status.DISCONNECTED: "red",
    Status.READY: "dim",
    Status.RUNNING: "dim",
    Status.WAITING_USER: "cyan",
    Status.APPROVAL_REQUIRED: "yellow",
    Status.COMPACTING: "dim",
    Status.INTERRUPTING: "dim",
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

    Urgent state comes from ``facts``; context is dropped as the terminal narrows.
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
        workspace=state.thread.workspace_root if state.thread and state.thread.workspace_root else workspace,
        thread_id=state.thread_id,
        thread_kind=state.thread_kind,
    )


def render_status_line(model: StatusLine, *, width: int) -> Text:
    """Render one row of at most ``width`` columns."""
    width = max(1, width)
    status = derive(model.facts)
    label = status_label(status, model.facts)
    segments: list[tuple[str, str]] = []
    if status not in {Status.READY, Status.RUNNING} or model.facts.jobs_running:
        segments.append((label, _BADGE_STYLE[status]))
    segments.extend(_optional_segments(model))
    rendered = Text()
    for text, style in segments:
        remaining = width - cell_len(rendered.plain)
        separator = 2 if rendered.plain else 0
        if cell_len(text) + separator > remaining:
            if not rendered.plain:
                rendered.append(_clip(text, remaining), style=style)
            break
        if separator:
            rendered.append("  ", style="dim")
        rendered.append(text, style=style)
    return rendered


def _optional_segments(model: StatusLine) -> Iterator[tuple[str, str]]:
    if model.thread_kind == SUBAGENT_THREAD_KIND:
        yield f"subagent:{model.thread_id}", "bold"
    if model.queue_depth:
        yield f"queued:{model.queue_depth}", "dim"
    if model.provider or model.model:
        yield "/".join(x for x in (model.provider, model.model) if x), ""
    if model.model_mode:
        yield model.model_mode, "dim"
    context = _context_segment(model)
    if context is not None:
        yield context
    for name, value in model.status_slots.items():
        yield f"{name}:{value}", "dim"


def _context_segment(model: StatusLine) -> tuple[str, str] | None:
    if model.context_window > 0 and model.context_input_tokens > 0:
        marker = "~" if model.context_input_estimated else ""
        overflow = model.context_input_tokens > model.context_window
        return (
            f"ctx:{marker}{_compact_count(model.context_input_tokens)}"
            f"/{_compact_count(model.context_window)}"
            f"{'!' if overflow else ''}",
            "red" if overflow else "",
        )
    return None


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
        content = render_status_line(model, width=width or self.size.width or 80)
        if self.content != content:
            self.update(content)


__all__ = [
    "STATUS_BAR_CSS", "StatusBar", "StatusLine",
    "render_status_line", "status_line_for"]
