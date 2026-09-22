"""The timeline: stable ids, in-place upsert, and a window that never drops.

Falsifying evidence for invariants I2 (committed content only changes through an
upsert of the same id) and I3 (entries entering the state are independent of
where the reader is looking). The old implementation keyed transcript entries by
list index and inferred deletions from eviction counters; every test here that
mentions "index" is asserting that positions are *derived*, never stored.
"""

from __future__ import annotations

import pytest

from XBotv2.tui.timeline import (
    AssistantEntry,
    Delivery,
    EntryKind,
    ErrorEntry,
    NoticeEntry,
    Timeline,
    ToolEntry,
    UserEntry,
)


def assistant(entry_id: str, content: str, *, streaming: bool = False, seq: int = 0) -> AssistantEntry:
    return AssistantEntry(id=entry_id, seq=seq, content=content, streaming=streaming)


def user(entry_id: str, content: str, *, seq: int = 0) -> UserEntry:
    return UserEntry(id=entry_id, seq=seq, content=content, delivery=Delivery.ACCEPTED)


# --- ordering and identity ------------------------------------------------


def test_new_entries_append_in_upsert_order() -> None:
    timeline = Timeline()
    timeline.upsert(user("u1", "hello"))
    timeline.upsert(assistant("a1", "hi"))
    assert timeline.ids() == ("u1", "a1")


def test_upsert_of_a_known_id_replaces_in_place() -> None:
    """Streaming must grow a message without moving it -- the defect was a
    streamed tail appended into an unrelated historical message."""
    timeline = Timeline()
    timeline.upsert(user("u1", "hello"))
    timeline.upsert(assistant("a1", "par"))
    timeline.upsert(user("u2", "steer"))
    timeline.upsert(assistant("a1", "partial answer", streaming=True))
    assert timeline.ids() == ("u1", "a1", "u2")
    entry = timeline.get("a1")
    assert isinstance(entry, AssistantEntry)
    assert entry.content == "partial answer"
    assert entry.streaming is True


def test_updating_one_entry_never_touches_another() -> None:
    timeline = Timeline()
    timeline.upsert(assistant("a1", "first"))
    timeline.upsert(assistant("a2", "second"))
    before = timeline.get("a1")
    timeline.upsert(assistant("a2", "second, longer"))
    assert timeline.get("a1") == before
    assert timeline.ids() == ("a1", "a2")


def test_get_returns_none_for_an_unknown_id() -> None:
    assert Timeline().get("nope") is None


def test_remove_reports_whether_it_removed_anything() -> None:
    timeline = Timeline()
    timeline.upsert(user("u1", "hello"))
    assert timeline.remove("u1") is True
    assert timeline.remove("u1") is False
    assert timeline.ids() == ()


def test_entries_expose_their_kind() -> None:
    assert user("u1", "x").kind is EntryKind.USER
    assert assistant("a1", "x").kind is EntryKind.ASSISTANT
    assert NoticeEntry(id="n1", seq=0, notice_kind="info", text="t").kind is EntryKind.NOTICE
    assert ErrorEntry(id="e1", seq=0, message="m").kind is EntryKind.ERROR
    assert ToolEntry(id="t1", seq=0, name="bash").kind is EntryKind.TOOL


# --- sequence allocation --------------------------------------------------


def test_next_seq_is_monotonic_and_unique() -> None:
    timeline = Timeline()
    seqs = [timeline.next_seq() for _ in range(3)]
    assert seqs == sorted(set(seqs))


# --- windows --------------------------------------------------------------


def test_tail_window_is_anchored_at_the_newest_entry() -> None:
    timeline = Timeline()
    for index in range(5):
        timeline.upsert(user(f"u{index}", str(index)))
    window = timeline.window(size=3)
    assert window.ids == ("u2", "u3", "u4")
    assert window.at_tail is True
    assert window.newer_count == 0
    assert window.total == 5


def test_window_anchored_at_an_entry_reports_what_is_below_it() -> None:
    """Paging back is a window anchored at an id, not an index mutation."""
    timeline = Timeline()
    for index in range(6):
        timeline.upsert(user(f"u{index}", str(index)))
    window = timeline.window(size=2, end="u2")
    assert window.ids == ("u1", "u2")
    assert window.at_tail is False
    assert window.newer_count == 3
    assert window.total == 6


def test_window_smaller_than_the_requested_size_is_clamped() -> None:
    timeline = Timeline()
    timeline.upsert(user("u0", "0"))
    window = timeline.window(size=10, end="u0")
    assert window.ids == ("u0",)
    assert window.start_index == 0
    assert window.at_tail is True


def test_unknown_anchor_fails_loudly() -> None:
    """Silently falling back to the tail would move the reader's position --
    the exact class of bug this rewrite removes."""
    timeline = Timeline()
    timeline.upsert(user("u0", "0"))
    with pytest.raises(KeyError):
        timeline.window(size=1, end="gone")


# --- I3: state growth is independent of the reader's position -------------


def test_entries_arriving_while_paged_back_are_retained_and_counted() -> None:
    timeline = Timeline()
    for index in range(4):
        timeline.upsert(user(f"u{index}", str(index)))
    reader = timeline.window(size=2, end="u1")
    assert reader.ids == ("u0", "u1")

    timeline.upsert(user("u4", "live while reading"))
    timeline.upsert(assistant("a5", "new answer", seq=timeline.next_seq()))

    after = timeline.window(size=2, end="u1")
    assert after.ids == ("u0", "u1"), "the reader's window must not move"
    assert after.newer_count == 4, "everything below the window is still there"
    assert timeline.get("u4") is not None, "live content must not be dropped"
    assert timeline.get("a5") is not None
    assert after.total == 6


def test_returning_to_the_tail_shows_everything_that_arrived() -> None:
    timeline = Timeline()
    timeline.upsert(user("u0", "0"))
    for index in range(1, 4):
        timeline.upsert(user(f"u{index}", str(index)))
    at_tail = timeline.window(size=10)
    assert at_tail.ids == ("u0", "u1", "u2", "u3")
    assert at_tail.at_tail is True
    assert at_tail.newer_count == 0


# --- a window must never silently re-anchor ------------------------------


def test_a_window_anchored_on_a_missing_entry_fails_loudly() -> None:
    """The caller that owns the window re-anchors; the timeline only fails.

    Silently falling back to the tail is what used to move the reader under
    their own feet when history was rewritten.
    """
    timeline = Timeline()
    for index in range(4):
        timeline.upsert(user(f"u{index}", str(index)))
    timeline.remove("u0")
    with pytest.raises(KeyError):
        timeline.window(size=2, end="u0")
    survivor = timeline.window(size=10, end="u2")
    assert survivor.ids == ("u1", "u2")
    assert survivor.at_tail is False


# --- payload fidelity -----------------------------------------------------


def test_tool_entry_keeps_its_full_result_and_status() -> None:
    timeline = Timeline()
    timeline.upsert(
        ToolEntry(
            id="call-1",
            seq=0,
            name="bash",
            args={"command": "ls"},
            status="running",
            started_at=1.0,
        )
    )
    timeline.upsert(
        ToolEntry(
            id="call-1",
            seq=0,
            name="bash",
            args={"command": "ls"},
            status="success",
            result="a\nb",
            started_at=1.0,
            finished_at=2.0,
        )
    )
    entry = timeline.get("call-1")
    assert isinstance(entry, ToolEntry)
    assert entry.status == "success"
    assert entry.result == "a\nb"
    assert entry.finished_at == 2.0
