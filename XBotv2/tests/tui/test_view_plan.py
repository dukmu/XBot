"""The transcript window is planned, not computed by index arithmetic.

The previous client kept ``window_start``/``window_end`` numbers, a mounted-widget
list, and three sets of eviction counters, and inferred what had happened by
diffing them. Every transcript defect came out of that arrangement.

Here the window is a pure function of what the timeline holds and what is
currently mounted. It returns a plan -- mount these, remove those -- and never
touches the timeline. Nothing outside the reader's view is dropped, and paging is
"anchor the window at another id", not "mutate a pair of integers".

Invariants under test:
  I2  an entry's widget is only ever created from, or updated with, its own entry
  I3  the plan is a function of ids; entries outside the window are untouched
"""

from __future__ import annotations

import pytest

from XBotv2.tui.view.plan import (
    UnknownAnchor,
    newer_anchor,
    older_anchor,
    plan_window,
)


def ids(count: int) -> tuple[str, ...]:
    return tuple(f"e{index}" for index in range(count))


# --- the tail window ------------------------------------------------------


def test_an_empty_timeline_plans_nothing() -> None:
    plan = plan_window((), (), limit=3)
    assert plan.mounted == ()
    assert plan.mount == ()
    assert plan.remove == ()
    assert plan.at_tail is True


def test_a_fresh_view_mounts_the_newest_entries_up_to_the_limit() -> None:
    plan = plan_window(ids(10), (), limit=4)
    assert plan.mounted == ("e6", "e7", "e8", "e9")
    assert plan.mount == ("e6", "e7", "e8", "e9")
    assert plan.remove == ()
    assert plan.at_tail is True


def test_a_short_timeline_mounts_everything() -> None:
    plan = plan_window(ids(2), (), limit=10)
    assert plan.mounted == ("e0", "e1")
    assert plan.at_tail is True


def test_planning_the_same_window_twice_changes_nothing() -> None:
    """Re-rendering must be idempotent; the old client re-mounted widgets the
    reader could already see."""
    first = plan_window(ids(6), (), limit=3)
    second = plan_window(ids(6), first.mounted, limit=3)
    assert second.mounted == first.mounted
    assert second.mount == ()
    assert second.remove == ()


# --- following the tail ---------------------------------------------------


def test_a_new_entry_at_the_tail_pays_for_itself_by_dropping_the_oldest() -> None:
    mounted = ("e0", "e1", "e2")
    plan = plan_window(("e0", "e1", "e2", "e3"), mounted, limit=3)
    assert plan.mounted == ("e1", "e2", "e3")
    assert plan.mount == ("e3",)
    assert plan.remove == ("e0",)
    assert plan.at_tail is True


def test_a_new_entry_while_paged_back_leaves_the_window_alone() -> None:
    """Live output must not move the reader, and must not be lost either: the
    plan says nothing about what the timeline keeps."""
    timeline = ("e0", "e1", "e2", "e3", "e4")
    anchored = plan_window(timeline[:3], (), anchor="e2", limit=2)
    assert anchored.mounted == ("e1", "e2")
    grown = timeline + ("e5",)
    after = plan_window(grown, anchored.mounted, anchor="e2", limit=2)
    assert after.mounted == ("e1", "e2")
    assert after.mount == ()
    assert after.remove == ()
    assert after.at_tail is False


def test_returning_to_the_tail_shows_everything_that_arrived() -> None:
    timeline = ("e0", "e1", "e2", "e3", "e4", "e5")
    plan = plan_window(timeline, ("e1", "e2"), anchor=None, limit=3)
    assert plan.mounted == ("e3", "e4", "e5")
    assert plan.mount == ("e3", "e4", "e5")
    assert plan.remove == ("e1", "e2")
    assert plan.at_tail is True


# --- paging ---------------------------------------------------------------


def test_an_anchored_window_ends_at_its_anchor() -> None:
    plan = plan_window(ids(10), (), anchor="e5", limit=3)
    assert plan.mounted == ("e3", "e4", "e5")
    assert plan.at_tail is False


def test_paging_up_keeps_the_overlap_and_drops_the_newest() -> None:
    """Reading up walks the held window; the mounted set stays bounded in both
    directions, which is what made long scrolls stutter before."""
    timeline = ids(10)
    first = plan_window(timeline, (), limit=3)
    assert first.mounted == ("e7", "e8", "e9")
    second = plan_window(timeline, first.mounted, anchor=older_anchor(timeline, first.mounted), limit=3)
    assert second.mounted == ("e4", "e5", "e6")
    assert second.mount == ("e4", "e5", "e6")
    assert second.remove == ("e7", "e8", "e9")


def test_paging_down_keeps_the_overlap_and_drops_the_oldest() -> None:
    timeline = ids(10)
    window = plan_window(timeline, (), anchor="e4", limit=3)
    assert window.mounted == ("e2", "e3", "e4")
    onward = plan_window(timeline, window.mounted, anchor=newer_anchor(timeline, window.mounted, limit=3), limit=3)
    assert onward.mounted == ("e5", "e6", "e7")
    assert onward.remove == ("e2", "e3", "e4")


def test_the_mounted_set_never_exceeds_the_limit_while_paging() -> None:
    timeline = ids(50)
    mounted: tuple[str, ...] = ()
    for _ in range(20):
        plan = plan_window(timeline, mounted, anchor=older_anchor(timeline, mounted) if mounted else "e39", limit=5)
        assert len(plan.mounted) <= 5, plan.mounted
        mounted = plan.mounted


def test_older_anchor_is_none_at_the_oldest_entry() -> None:
    timeline = ids(3)
    assert older_anchor(timeline, ("e0", "e1")) is None


def test_newer_anchor_is_none_once_the_window_reaches_the_tail() -> None:
    timeline = ids(6)
    assert newer_anchor(timeline, ("e3", "e4", "e5"), limit=3) is None


def test_newer_anchor_moves_one_window_at_a_time() -> None:
    timeline = ids(20)
    assert newer_anchor(timeline, ("e0", "e1", "e2"), limit=3) == "e5"


def test_anchors_on_an_empty_or_unmounted_timeline_are_none() -> None:
    assert older_anchor((), ()) is None
    assert newer_anchor((), (), limit=3) is None


# --- the mounted set must stay coherent -----------------------------------


def test_a_hole_in_the_mounted_set_is_repaired_by_replacing_it() -> None:
    """A gap would leave the reader looking at a transcript with a piece
    missing; the old client simply kept the gap."""
    plan = plan_window(ids(6), ("e2", "e4"), limit=4)
    assert plan.mounted == ("e2", "e3", "e4", "e5")
    assert plan.remove == ("e2", "e4")
    assert plan.mount == ("e2", "e3", "e4", "e5")


def test_entries_the_timeline_no_longer_holds_are_removed() -> None:
    plan = plan_window(("e0", "e1", "e2"), ("e0", "e1", "e2", "e9"), limit=5)
    assert "e9" in plan.remove
    assert plan.mounted == ("e0", "e1", "e2")


def test_a_wholly_stale_mounted_set_is_replaced() -> None:
    plan = plan_window(("e5", "e6"), ("e0", "e1"), limit=5)
    assert plan.remove == ("e0", "e1")
    assert plan.mount == ("e5", "e6")
    assert plan.mounted == ("e5", "e6")


def test_an_unknown_anchor_fails_loudly() -> None:
    """Silently re-anchoring is how the reader used to be moved without asking."""
    with pytest.raises(UnknownAnchor):
        plan_window(ids(3), (), anchor="gone", limit=2)


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        plan_window(ids(3), (), limit=0)


# --- payload fidelity -----------------------------------------------------


def test_the_plan_never_names_an_entry_it_did_not_receive() -> None:
    timeline = ids(4)
    plan = plan_window(timeline, ("e0",), anchor="e2", limit=2)
    assert set(plan.mounted) <= set(timeline)
    assert set(plan.mount) <= set(timeline)


def test_a_stale_neighbour_is_removed_without_touching_the_live_entries() -> None:
    """Only a genuine hole forces a replacement; a stale neighbour does not."""
    plan = plan_window(ids(6), ("e1", "e3"), limit=4)
    assert plan.mounted == ("e2", "e3", "e4", "e5")
    assert plan.remove == ("e1",)
    assert plan.mount == ("e2", "e4", "e5")


def test_a_window_that_only_shrinks_removes_the_excess() -> None:
    plan = plan_window(("e0", "e1"), ("e0", "e1"), anchor="e1", limit=2)
    assert plan.mounted == ("e0", "e1")
    plan = plan_window(("e1",), ("e0", "e1"), anchor="e1", limit=2)
    assert plan.mounted == ("e1",)
    assert plan.remove == ("e0",)
