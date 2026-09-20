"""Protocol stress: paging a large trajectory must stay bounded per request.

A windowed client walks a long conversation backwards page by page. If any of
that work scaled with the whole history — loading every record to slice it,
re-counting positions, or replaying the cursor chain from the start — this test
would show it as total time growth far beyond the linear page count. The
absolute ceiling is deliberately generous so it catches complexity regressions
rather than machine noise.
"""

from __future__ import annotations

import time

import pytest

from XBotv2.core.history import HistoryCursorInvalid
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths
from XBotv2.persistence.store import ThreadPersistence

RECORDS = 5_000
PAGE = 200
#: 25 pages of 200 records; generous enough to catch O(n^2) rather than noise.
MAX_TOTAL_SECONDS = 10.0


def _persistence(tmp_path) -> ThreadPersistence:
    return ThreadPersistence.create(
        RuntimePaths.from_data_dir(tmp_path).session("stress"),
        thread_id="t1",
        workspace_root="/workspace",
        provider="default",
    )


def _seed(persistence: ThreadPersistence, count: int) -> None:
    for start in range(0, count, 500):
        size = min(500, count - start)
        persistence.history.append([
            Message(role="user", content=f"m{start + index}")
            for index in range(size)
        ])


def test_trajectory_paging_over_a_large_history_is_bounded(tmp_path):
    persistence = _persistence(tmp_path)
    _seed(persistence, RECORDS)

    started = time.perf_counter()
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = persistence.history.page_trajectory(limit=PAGE, cursor=cursor)
        pages += 1
        assert len(page.items) <= PAGE, "a page must not exceed the requested size"
        assert page.newest_position == RECORDS
        seen.extend(item.position for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages <= RECORDS // PAGE + 1, "paging must terminate in page count"
    elapsed = time.perf_counter() - started

    # Every record is reachable exactly once, with no gaps and no duplicates;
    # pages arrive newest-first but each page preserves append order.
    assert sorted(seen) == list(range(1, RECORDS + 1))
    assert seen[:PAGE] == list(range(RECORDS - PAGE + 1, RECORDS + 1))
    assert pages == RECORDS // PAGE
    assert elapsed < MAX_TOTAL_SECONDS, (
        f"paging {RECORDS} records took {elapsed:.2f}s; "
        "per-page work is scaling with history length"
    )


def test_positional_anchoring_matches_the_cursor_walk(tmp_path):
    """The windowed client's anchor must find exactly what the cursor walk does."""
    persistence = _persistence(tmp_path)
    _seed(persistence, RECORDS)

    by_cursor: list[int] = []
    cursor: str | None = None
    while True:
        page = persistence.history.page_trajectory(limit=PAGE, cursor=cursor)
        by_cursor.extend(item.position for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    # Walk forwards in the same direction a windowed client does: always anchor
    # on the oldest position still held.
    by_anchor: list[int] = []
    before: int | None = None
    while True:
        page = persistence.history.page_trajectory(limit=PAGE, before=before)
        by_anchor.extend(item.position for item in page.items)
        if not page.items or page.items[0].position == 1:
            break
        before = page.items[0].position

    assert by_anchor == by_cursor

    with pytest.raises(HistoryCursorInvalid):
        persistence.history.page_trajectory(limit=PAGE, before=RECORDS + 2)
