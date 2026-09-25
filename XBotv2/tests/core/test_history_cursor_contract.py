"""History pagination uses typed cursors internally and complete page values."""

from pydantic import TypeAdapter

from XBotv2.core.domain import Cursor, HistoryRevision
from XBotv2.core.history import (
    HistoryPage,
    TrajectoryRead,
    decode_history_cursor,
    encode_history_cursor,
)


def test_cursor_is_typed_in_domain_but_remains_a_string_on_the_wire():
    revision = HistoryRevision("session/thread:transcript:4")
    cursor = encode_history_cursor(revision, 12)

    assert isinstance(cursor, str)
    assert decode_history_cursor(cursor, revision) == 12
    assert decode_history_cursor(Cursor(str(cursor)), revision) == 12

    page = HistoryPage(items=("message",), older_cursor=cursor)
    adapter = TypeAdapter(HistoryPage[str])
    wire = adapter.dump_python(page, mode="json")
    assert wire == {"items": ["message"], "older_cursor": str(cursor)}
    assert adapter.validate_python(wire) == page


def test_trajectory_read_requires_a_real_tail_position():
    page = HistoryPage(items=(), older_cursor=None)
    try:
        TrajectoryRead(page=page)
    except TypeError:
        pass
    else:
        raise AssertionError("newest_position must be supplied")

    empty = TrajectoryRead(page=page, newest_position=0)
    adapter = TypeAdapter(TrajectoryRead)
    assert adapter.validate_python(adapter.dump_python(empty, mode="json")) == empty
