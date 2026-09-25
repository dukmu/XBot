"""Persistence stores canonical messages in one append-only trajectory."""

import json

import pytest

from XBotv2.core.domain import (
    InputId,
    MessageId,
    TransactionCommitted,
    TransactionEnded,
    TransactionRef,
    TransactionStarted,
)
from XBotv2.core.history import HistoryCursorInvalid, MessageAppended, SurfaceReplaced
from XBotv2.core.messages import CompactionSummaryMessage, HumanInputMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.paths import RuntimePaths
from XBotv2.persistence.store import ThreadPersistence


def _store(tmp_path):
    return ThreadPersistence.create(
        RuntimePaths.from_data_dir(tmp_path).session("s1"),
        thread_id="agent",
    )


def _human(index: int, text: str | None = None) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId(f"message-{index}"),
        input_id=InputId(f"input-{index}"),
        parts=(TextPart(text=text or f"message {index}"),),
    )


def test_append_round_trips_canonical_message_identity(tmp_path):
    persistence = _store(tmp_path)
    messages = (_human(1), _human(2))
    persistence.history.append(messages)

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    assert reopened.history.load_surface() == messages
    assert reopened.history.count() == 2


def test_trajectory_rejects_duplicate_message_identity(tmp_path):
    history = _store(tmp_path).history
    history.append((_human(1),))
    with pytest.raises(ValueError, match="unique identities"):
        history.append((_human(1, "different content"),))


def test_surface_replacement_and_transcript_are_separate_projections(tmp_path):
    history = _store(tmp_path).history
    original = (_human(1), _human(2), _human(3))
    history.append(original)
    summary = CompactionSummaryMessage(
        id=MessageId("summary-1"), summary="older context",
    )

    history.replace_surface(
        (original[0].id, original[1].id),
        (summary,),
        operation="compact",
        preserve_transcript=True,
    )

    assert history.load_surface() == (summary, original[2])
    assert history.load_transcript() == list(original)
    assert isinstance(history.page_trajectory(limit=1).page.items[0], SurfaceReplaced)


def test_non_preserving_replacement_updates_both_projections(tmp_path):
    history = _store(tmp_path).history
    original = (_human(1), _human(2))
    history.append(original)
    replacement = _human(3, "replacement")
    history.replace_surface(
        tuple(message.id for message in original),
        (replacement,),
        operation="clear",
        preserve_transcript=False,
    )
    assert history.load_surface() == (replacement,)
    assert history.load_transcript() == [replacement]


def test_trajectory_paging_reports_tail_and_validates_anchor(tmp_path):
    history = _store(tmp_path).history
    history.append(tuple(_human(index) for index in range(1, 6)))

    page = history.page_trajectory(limit=2)
    assert page.newest_position == 5
    assert [entry.position for entry in page.page.items] == [4, 5]
    assert all(isinstance(entry, MessageAppended) for entry in page.page.items)
    older = history.page_trajectory(limit=2, cursor=page.page.older_cursor)
    assert [entry.position for entry in older.page.items] == [2, 3]
    with pytest.raises(HistoryCursorInvalid):
        history.page_trajectory(limit=2, before=99)


def test_open_transactions_fold_typed_start_and_end_events(tmp_path):
    history = _store(tmp_path).history
    transaction = TransactionRef(kind="compaction", id="tx-1")
    history.record(TransactionStarted(transaction=transaction), durable=True)
    assert history.open_transactions("compaction") == frozenset({"tx-1"})
    history.record(TransactionEnded(
        transaction=transaction,
        outcome=TransactionCommitted(),
    ), durable=True)
    assert history.open_transactions("compaction") == frozenset()


def test_corrupt_trajectory_fails_at_persistence_boundary(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1),))
    with persistence.history.path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    with pytest.raises(ValueError, match="Invalid messages.jsonl"):
        reopened.history.load_surface()


def _rewrite_records(path, mutate):
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    mutate(records)
    # Change file size so the shared trajectory reader must discard its cache.
    path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False) + " "
            for record in records
        ) + "\n",
        encoding="utf-8",
    )


def test_trajectory_rejects_position_gap_when_reloaded(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1), _human(2)))
    _rewrite_records(
        persistence.history.path,
        lambda records: records[1]["entry"].update(position=9),
    )

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    with pytest.raises(ValueError, match="positions must be contiguous"):
        reopened.history.load_surface()


def test_trajectory_rejects_unknown_replacement_source_when_reloaded(tmp_path):
    persistence = _store(tmp_path)
    original = _human(1)
    persistence.history.append((original,))
    persistence.history.replace_surface(
        (original.id,),
        (_human(2, "replacement"),),
        operation="replace",
        preserve_transcript=False,
    )
    _rewrite_records(
        persistence.history.path,
        lambda records: records[1]["entry"].update(
            source_ids=["not-in-the-current-surface"]
        ),
    )

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    with pytest.raises(ValueError, match="source nodes are not current"):
        reopened.history.load_surface()


def test_trajectory_rejects_reused_message_identity_when_reloaded(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1), _human(2)))
    _rewrite_records(
        persistence.history.path,
        lambda records: records[1]["entry"]["message"].update(
            id="message-1"
        ),
    )

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    with pytest.raises(ValueError, match="reuses a message identity"):
        reopened.history.load_surface()


def test_trajectory_rejects_legacy_flat_record_without_entry_envelope(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1),))
    path = persistence.history.path
    current = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    legacy = {"schema_version": current["schema_version"], **current["entry"]}
    path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    with pytest.raises(ValueError, match="entry"):
        reopened.history.load_surface()
