"""The job panel: rows derived from job snapshots, reconciled by id.

Background work is part of "is the agent busy", so this panel and the status line
must agree. The old panel rebuilt its widgets on every tick, which is what made
expanded rows flicker and collapse; here rows are reconciled by ``job_id``.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from XBotv2.jobs.contracts import JobSnapshot
from XBotv2.tui.view.jobs import JobPanel, job_row, order_jobs


def job(
    job_id: str = "j1",
    *,
    kind: str = "shell",
    status: str = "running",
    command: str = "sleep 1",
    started_at: float = 0.0,
    finished_at: float = 0.0,
    output: str = "",
    error: str = "",
    agent: str = "",
) -> JobSnapshot:
    return JobSnapshot(
        job_id=job_id,
        kind=kind,
        status=status,
        command=command,
        cwd="/w",
        created_at=0.0,
        started_at=started_at,
        finished_at=finished_at,
        output=output,
        error=error,
        agent=agent,
    )


class Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield JobPanel(id="jobs")

    def on_mount(self) -> None:
        self.panel = self.query_one("#jobs", JobPanel)


# --- ordering -------------------------------------------------------------


def test_running_jobs_come_before_finished_ones() -> None:
    ordered = order_jobs(
        [job("done", status="completed", finished_at=5.0), job("busy", status="running")]
    )
    assert [item.job_id for item in ordered] == ["busy", "done"]


def test_finished_jobs_are_ordered_most_recent_first() -> None:
    ordered = order_jobs(
        [
            job("older", status="completed", finished_at=1.0),
            job("newer", status="completed", finished_at=9.0),
        ]
    )
    assert [item.job_id for item in ordered] == ["newer", "older"]


def test_pending_counts_as_unfinished() -> None:
    ordered = order_jobs([job("done", status="stopped"), job("queued", status="pending")])
    assert ordered[0].job_id == "queued"


def test_ordering_is_stable_for_equal_keys() -> None:
    jobs = [job("a", status="running"), job("b", status="running")]
    assert [item.job_id for item in order_jobs(jobs)] == ["a", "b"]


# --- rows -----------------------------------------------------------------


def test_a_row_names_the_job_its_kind_and_its_state() -> None:
    row = job_row(job("j7", kind="agent", status="running", command="review the diff"))
    assert "j7" in row
    assert "agent" in row
    assert "running" in row
    assert "review the diff" in row


def test_a_finished_row_shows_how_long_it_took() -> None:
    assert "2.5s" in job_row(job(status="completed", started_at=10.0, finished_at=12.5))


def test_an_unfinished_row_shows_no_duration() -> None:
    assert "s " not in f"{job_row(job(status='running', started_at=10.0))} "


def test_a_failed_row_shows_the_error() -> None:
    row = job_row(job(status="failed", error="exit code 1"))
    assert "exit code 1" in row


def test_a_finished_row_shows_a_tail_of_its_output() -> None:
    row = job_row(job(status="completed", output="line one\nline two\nline three"))
    assert "line three" in row


def test_a_row_never_exceeds_the_width_it_is_given() -> None:
    long = job(command="x" * 400, output="y" * 400, error="z" * 400, status="failed")
    for width in (24, 40, 80):
        for line in job_row(long, width=width).splitlines():
            assert len(line) <= width, (width, line)


def test_a_row_is_never_empty_for_a_sparse_job() -> None:
    assert job_row(job(command="")).strip()


# --- the panel reconciles -------------------------------------------------


async def test_the_panel_shows_one_row_per_job() -> None:
    app = Harness()
    async with app.run_test(size=(80, 12)) as pilot:
        app.panel.show([job("j1"), job("j2", status="completed", finished_at=1.0)], width=80)
        await pilot.pause()
        assert app.panel.rows == 2


async def test_updating_a_job_reuses_its_row() -> None:
    """Rebuilding the list on every tick is what made the panel flicker."""
    app = Harness()
    async with app.run_test(size=(80, 12)) as pilot:
        app.panel.show([job("j1", status="running")], width=80)
        await pilot.pause()
        first = app.panel.row_widget("j1")
        app.panel.show([job("j1", status="completed", finished_at=1.0)], width=80)
        await pilot.pause()
        assert app.panel.row_widget("j1") is first
        assert "completed" in app.panel.row_text("j1")


async def test_a_vanished_job_loses_its_row() -> None:
    app = Harness()
    async with app.run_test(size=(80, 12)) as pilot:
        app.panel.show([job("j1"), job("j2")], width=80)
        await pilot.pause()
        app.panel.show([job("j2")], width=80)
        await pilot.pause()
        assert app.panel.rows == 1
        assert app.panel.row_widget("j1") is None


async def test_the_panel_follows_the_model_order() -> None:
    app = Harness()
    async with app.run_test(size=(80, 12)) as pilot:
        app.panel.show([job("done", status="completed", finished_at=1.0), job("busy")], width=80)
        await pilot.pause()
        assert app.panel.order == ("busy", "done")


async def test_an_empty_panel_shows_nothing() -> None:
    app = Harness()
    async with app.run_test(size=(80, 12)) as pilot:
        app.panel.show([], width=80)
        await pilot.pause()
        assert app.panel.rows == 0
