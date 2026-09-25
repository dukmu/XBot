"""The background job panel.

Rows are derived from job snapshots (``order_jobs``/``job_row`` are pure) and the
widget reconciles them by ``job_id``. Rebuilding the list on every tick is what
made expanded rows flicker and collapse in the previous client, so rows are
created once and updated in place.
"""

from __future__ import annotations

from typing import Sequence

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from XBotv2.jobs.contracts import JobView

_UNFINISHED = frozenset({"queued", "running"})

# A glyph marks the state visually; the state *word* is the server's own, so the
# panel cannot drift from the vocabulary the protocol uses.
_GLYPHS: dict[str, str] = {
    "queued": "·",
    "running": "▶",
    "succeeded": "✔",
    "failed_before_start": "✖",
    "failed_running": "✖",
    "cancelled_before_start": "■",
    "cancelled_running": "■",
}

JOB_PANEL_CSS = """
JobPanel {
    height: 1fr;
    width: 1fr;
}
JobPanel .job-row {
    height: auto;
    width: 1fr;
}
"""


def order_jobs(jobs: Sequence[JobView]) -> tuple[JobView, ...]:
    """Unfinished work first, then finished work most-recent-first.

    The user cares about what is still running; a stable sort keeps equal keys in
    the order the server reported them.
    """
    unfinished = [job for job in jobs if job.state in _UNFINISHED]
    finished = [job for job in jobs if job.state not in _UNFINISHED]
    finished.sort(key=lambda job: job.elapsed_ms, reverse=True)
    return tuple(unfinished) + tuple(finished)


def job_row(job: JobView, *, width: int = 80) -> str:
    """One job as text: state, identity, command, and a detail line when useful.

    Every line is clipped to ``width``: a job row that overflows would push the
    panel's layout out of shape.
    """
    glyph = _GLYPHS.get(job.state, "?")
    kind = job.kind
    elapsed = _elapsed(job)
    head = f"{glyph}  {job.id}  {kind}  {job.state}  {job.label}".rstrip()
    if elapsed:
        head = f"{head}  {elapsed}"
    lines = [_clip(head, width)]
    detail = job.summary.strip() if job.summary else ""
    if detail:
        lines.append(_clip(f"      {detail}", width))
    return "\n".join(lines)


def _elapsed(job: JobView) -> str:
    if job.state in _UNFINISHED:
        return ""
    return f"{job.elapsed_ms / 1000:.1f}s"


def _clip(text: str, width: int) -> str:
    width = max(1, width)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3]}..."


class JobPanel(VerticalScroll):
    """A reconciled list of job rows."""

    DEFAULT_CSS = JOB_PANEL_CSS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._widgets: dict[str, Static] = {}
        self._order: tuple[str, ...] = ()

    @property
    def rows(self) -> int:
        return len(self._widgets)

    @property
    def order(self) -> tuple[str, ...]:
        return self._order

    def row_widget(self, job_id: str) -> Static | None:
        return self._widgets.get(job_id)

    def row_text(self, job_id: str) -> str:
        widget = self._widgets.get(job_id)
        if widget is None:
            return ""
        return str(widget.content)

    def show(self, jobs: Sequence[JobView], *, width: int) -> None:
        """Render ``jobs``, updating existing rows instead of rebuilding them."""
        ordered = order_jobs(jobs)
        wanted = {job.id for job in ordered}
        for job_id in [job_id for job_id in self._widgets if job_id not in wanted]:
            widget = self._widgets.pop(job_id)
            widget.remove()
        for job in ordered:
            text = job_row(job, width=width)
            widget = self._widgets.get(job.id)
            if widget is None:
                widget = Static(Text(text), classes="job-row")
                self._widgets[job.id] = widget
                self.mount(widget)
            else:
                widget.update(Text(text))
        self._order = tuple(job.id for job in ordered)
        self._reorder()

    def _reorder(self) -> None:
        for index, job_id in enumerate(self._order):
            widget = self._widgets.get(job_id)
            if widget is None:
                continue
            if index < len(self.children) and self.children[index] is widget:
                continue
            self.move_child(widget, before=index)


__all__ = ["JOB_PANEL_CSS", "JobPanel", "job_row", "order_jobs"]
