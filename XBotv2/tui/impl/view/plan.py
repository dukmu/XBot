"""The transcript window, as a pure plan.

A window is "the last ``limit`` ids ending at ``anchor``" (or at the newest entry
when ``anchor`` is ``None``). Planning it is a function of what the timeline holds
and what is currently mounted; it returns which ids to mount and which to remove,
and it never looks at, or mutates, the timeline.

Two properties this buys:

* paging is *anchoring at another id*, so nothing is derived from a pair of
  integers that several code paths had to keep in step;
* what the state holds is unaffected by where the reader is looking, so live
  output cannot be dropped for arriving while the reader is scrolled back.

The mounted set is required to stay contiguous. A genuine hole (a missing id
between two mounted ones) is repaired by replacing the mounted set, because
silently keeping a gap is how the reader ends up looking at an incomplete
transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class UnknownAnchor(KeyError):
    """The window was asked to end at an id the timeline does not hold."""


@dataclass(frozen=True)
class ViewPlan:
    """Which ids the view should be showing after this render."""

    mounted: tuple[str, ...]
    mount: tuple[str, ...]
    remove: tuple[str, ...]
    at_tail: bool


def plan_window(
    ids: Sequence[str],
    mounted: Sequence[str],
    *,
    anchor: str | None = None,
    limit: int,
) -> ViewPlan:
    """Plan the mounted set for a window ending at ``anchor``.

    Raises ``UnknownAnchor`` rather than falling back to the tail: moving the
    reader without being asked is the behaviour this rewrite removes.
    """
    if limit < 1:
        raise ValueError("window limit must be positive")
    ordered = tuple(ids)
    current = tuple(mounted)
    end = len(ordered) if anchor is None else _index_of(ordered, anchor) + 1
    start = max(0, end - limit)
    window = ordered[start:end]
    at_tail = end == len(ordered)

    inside = tuple(entry_id for entry_id in current if entry_id in window)
    if inside and not _is_contiguous(window, inside):
        return ViewPlan(mounted=window, mount=window, remove=current, at_tail=at_tail)

    return ViewPlan(
        mounted=window,
        mount=tuple(entry_id for entry_id in window if entry_id not in current),
        remove=tuple(entry_id for entry_id in current if entry_id not in window),
        at_tail=at_tail,
    )


def older_anchor(ids: Sequence[str], window_ids: Sequence[str]) -> str | None:
    """The anchor one window older, or ``None`` when already at the oldest entry."""
    if not ids or not window_ids:
        return None
    first = window_ids[0]
    index = _index_of(ids, first, missing=None)
    if index is None or index == 0:
        return None
    return ids[index - 1]


def newer_anchor(
    ids: Sequence[str],
    window_ids: Sequence[str],
    *,
    limit: int,
) -> str | None:
    """The anchor one window newer, or ``None`` when already at the tail.

    The returned anchor may be the newest id, which simply means the next window
    *is* the tail.
    """
    if not ids or not window_ids:
        return None
    last = window_ids[-1]
    index = _index_of(ids, last, missing=None)
    if index is None:
        return None
    if index == len(ids) - 1:
        return None
    target = min(len(ids) - 1, index + max(1, limit))
    if target <= index or target == len(ids) - 1:
        # The next window is the tail, which the anchor represents as ``None``.
        return None
    return ids[target]


def _index_of(ids: Sequence[str], entry_id: str, missing: type[Exception] | None = UnknownAnchor) -> int:
    try:
        return tuple(ids).index(entry_id)
    except ValueError:
        if missing is None:
            raise
        raise UnknownAnchor(entry_id) from None


def _is_contiguous(window: Sequence[str], inside: Sequence[str]) -> bool:
    """Whether ``inside`` is a run of consecutive ids from ``window``."""
    if len(set(inside)) != len(inside):
        return False
    positions = [window.index(entry_id) for entry_id in inside]
    return positions == list(range(positions[0], positions[0] + len(positions)))


__all__ = ["UnknownAnchor", "ViewPlan", "newer_anchor", "older_anchor", "plan_window"]
