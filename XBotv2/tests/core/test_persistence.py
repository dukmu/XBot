"""Persistence stores canonical messages in one append-only trajectory."""

import json
import os
from pathlib import Path

import pytest

from XBotv2.core.domain import (
    MessageId,
    TransactionCommitted,
    TransactionEnded,
    TransactionRef,
    TransactionStarted,
)
from XBotv2.core.history import ConversationHistory, HistoryCursorInvalid, MessageAppended, SurfaceReplaced
from XBotv2.core.messages import CompactionSummaryMessage, HumanInputMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.paths import RuntimePaths
from XBotv2.persistence.contracts import ThreadFailed, ThreadStarted
from XBotv2.persistence.models import StoredTrajectoryRecord
from XBotv2.persistence.store import ThreadPersistence, _TrajectoryState


def _store(tmp_path):
    return ThreadPersistence.open(
        RuntimePaths.from_data_dir(tmp_path).session("s1"),
        thread_id="agent",
    )


def _human(index: int, text: str | None = None) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId(f"message-{index}"),
        parts=(TextPart(text=text or f"message {index}"),),
    )


def test_append_round_trips_canonical_message_identity(tmp_path):
    persistence = _store(tmp_path)
    messages = (_human(1), _human(2))
    persistence.history.append(messages)

    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    assert reopened.history.load_surface() == messages
    assert reopened.history.count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [20, 40])
async def test_inbox_growth_writes_only_new_records(tmp_path, monkeypatch, count):
    import XBotv2.persistence.store as storage
    from XBotv2.agentloop import AgentInbox
    from XBotv2.agentloop.contracts import HumanInput, InboxItem
    from XBotv2.core.domain import InboxTarget

    class Events:
        async def emit(self, *args):
            pass

    persistence = _store(tmp_path)
    inbox = AgentInbox(events=Events(), sink=persistence.inbox)
    written = 0
    atomic_write = storage.write_text_atomic
    raw_write = os.write

    def measure_atomic(path, content):
        nonlocal written
        if path == persistence.paths.inbox_file:
            written += len(content.encode("utf-8"))
        return atomic_write(path, content)

    def measure_append(fd, content):
        nonlocal written
        result = raw_write(fd, content)
        written += result
        return result

    monkeypatch.setattr(storage, "write_text_atomic", measure_atomic)
    monkeypatch.setattr(os, "write", measure_append)
    for index in range(count):
        await inbox.submit(InboxItem(
            id=f"input-{index}", target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="payload" * 100),
        ), wake=False)
    assert written == persistence.paths.inbox_file.stat().st_size
    before_claim = persistence.paths.inbox_file.read_bytes()
    await inbox.claim_turn()
    assert persistence.paths.inbox_file.read_bytes() == before_claim
    reopened = _store(tmp_path)
    assert len(reopened.inbox.load()) == count


@pytest.mark.asyncio
async def test_inbox_trace_replays_mutations_without_rewriting_prefix(tmp_path):
    from XBotv2.agentloop import AgentInbox
    from XBotv2.agentloop.contracts import HumanInput, InboxItem
    from XBotv2.core.domain import InboxTarget

    class Events:
        async def emit(self, *args):
            pass

    store = _store(tmp_path)
    inbox = AgentInbox(events=Events(), sink=store.inbox)
    for identity in ("first", "second", "third"):
        await inbox.submit(InboxItem(id=identity, target=InboxTarget.NEXT_TURN,
                                    input=HumanInput(content=identity)), wake=False)
    prefix = store.paths.inbox_file.read_bytes()
    await inbox.edit("first", "corrected")
    await inbox.retarget("first", InboxTarget.NEXT_STEP)
    await inbox.remove("second")
    restored = _store(tmp_path).inbox.load()
    assert [(item.id, item.input.content, item.target) for item in restored] == [
        ("first", "corrected", InboxTarget.NEXT_STEP),
        ("third", "third", InboxTarget.NEXT_TURN),
    ]
    claimed = await inbox.claim_step()
    await inbox.commit([item.id for item in claimed])
    assert [item.id for item in _store(tmp_path).inbox.load()] == ["third"]
    await inbox.discard()
    assert _store(tmp_path).inbox.load() == []
    assert store.paths.inbox_file.read_bytes().startswith(prefix)
    records = [json.loads(line)["change"] for line in store.paths.inbox_file.read_text().splitlines()]
    assert [record["kind"] for record in records] == [
        "inserted", "inserted", "inserted", "edited", "retargeted", "removed", "consumed", "discarded",
    ]
    assert records[-2] == {"kind": "consumed", "ids": ["first"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_failed_inbox_append_preserves_prefix_and_allows_retry(tmp_path, monkeypatch, partial):
    from XBotv2.agentloop import AgentInbox
    from XBotv2.agentloop.contracts import HumanInput, InboxItem
    from XBotv2.core.domain import InboxTarget

    class Events:
        async def emit(self, *args):
            pass

    store = _store(tmp_path)
    inbox = AgentInbox(events=Events(), sink=store.inbox)
    first = InboxItem(id="first", target=InboxTarget.NEXT_TURN, input=HumanInput(content="first"))
    await inbox.submit(first, wake=False)
    before = store.paths.inbox_file.read_bytes()
    original_write = os.write
    calls = 0

    def fail(fd, content):
        nonlocal calls
        calls += 1
        if partial and calls == 1:
            return original_write(fd, content[:len(content) // 2])
        raise OSError("write failed")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail)
        with pytest.raises(OSError, match="write failed"):
            await inbox.edit("first", "changed")
    assert inbox.pending == [first]
    assert store.paths.inbox_file.read_bytes() == before
    assert _store(tmp_path).inbox.load() == [first]
    await inbox.edit("first", "changed")
    assert _store(tmp_path).inbox.load()[0].input.content == "changed"


def test_failed_first_inbox_write_does_not_materialize_a_thread(tmp_path, monkeypatch):
    from XBotv2.agentloop.contracts import HumanInput, InboxItem, Inserted
    from XBotv2.core.domain import InboxTarget
    from XBotv2.session.manager import thread_has_evidence

    store = _store(tmp_path)

    def fail(fd, content):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", fail)
    with pytest.raises(OSError, match="disk full"):
        store.inbox.append(Inserted(item=InboxItem(
            id="failed", target=InboxTarget.NEXT_TURN, input=HumanInput(content="failed"),
        ), wake=False))
    assert not store.has_persisted_state()
    assert not thread_has_evidence(store.paths.session, store.thread_id)


def test_inbox_rejects_invalid_transition_before_append(tmp_path):
    from XBotv2.agentloop.contracts import HumanInput, InboxItem, Inserted, Consumed
    from XBotv2.core.domain import InboxTarget

    store = _store(tmp_path)
    item = InboxItem(id="one", target=InboxTarget.NEXT_TURN, input=HumanInput(content="one"))
    store.inbox.append(Inserted(item=item, wake=False))
    prefix = store.paths.inbox_file.read_bytes()
    for ids in (("missing",), ("one", "one")):
        with pytest.raises(ValueError, match="distinct pending"):
            store.inbox.append(Consumed(ids=ids))
        assert store.paths.inbox_file.read_bytes() == prefix
        assert store.inbox.load() == [item]
    store.inbox.append(Consumed(ids=("one",)))
    assert _store(tmp_path).inbox.load() == []


def test_inbox_drops_uncommitted_tail_before_next_append(tmp_path):
    from XBotv2.agentloop.contracts import HumanInput, InboxItem, Inserted, Consumed
    from XBotv2.core.domain import InboxTarget

    store = _store(tmp_path)
    item = InboxItem(id="one", target=InboxTarget.NEXT_TURN, input=HumanInput(content="one"))
    store.inbox.append(Inserted(item=item, wake=False))
    prefix = store.paths.inbox_file.read_bytes()
    with store.paths.inbox_file.open("ab") as stream:
        stream.write(b'{"version":1,"change":')
    reopened = _store(tmp_path)
    assert reopened.inbox.load() == [item]
    reopened.inbox.append(Consumed(ids=("one",)))
    assert _store(tmp_path).inbox.load() == []
    assert store.paths.inbox_file.read_bytes().startswith(prefix)
    assert len(store.paths.inbox_file.read_bytes().splitlines()) == 2


@pytest.mark.parametrize("operation", ["append", "replace"])
def test_history_rejects_duplicate_identity_before_writing_trace(tmp_path, operation):
    persistence = _store(tmp_path)
    history = ConversationHistory(sink=persistence.history)
    history.append(_human(1))
    before = persistence.paths.messages_file.read_bytes()
    invalid = _human(1, "different content")
    with pytest.raises(ValueError, match="identities"):
        if operation == "append":
            history.append(invalid)
        else:
            history.replace_range(0, 1, (invalid,), operation="replace")
    assert persistence.paths.messages_file.read_bytes() == before
    assert history.snapshot() == (_human(1),)
    assert persistence.history.load_surface() == history.snapshot()


@pytest.mark.parametrize("operation", ["append", "replace"])
def test_history_write_failure_does_not_claim_identity(tmp_path, monkeypatch, operation):
    persistence = _store(tmp_path)
    history = ConversationHistory(sink=persistence.history)
    history.append(_human(1))
    before = persistence.paths.messages_file.read_bytes()

    def mutate():
        if operation == "append":
            history.append(_human(2))
        else:
            history.replace_range(0, 1, (_human(2),), operation="replace")

    def reject_write(descriptor, payload):
        raise OSError("write failed")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", reject_write)
        with pytest.raises(OSError, match="write failed"):
            mutate()
    assert persistence.paths.messages_file.read_bytes() == before
    assert history.snapshot() == persistence.history.load_surface() == (_human(1),)
    mutate()
    expected = (_human(1), _human(2)) if operation == "append" else (_human(2),)
    assert history.snapshot() == persistence.history.load_surface() == expected


@pytest.mark.parametrize("turns", [20, 40])
def test_live_threads_append_and_page_without_rereading_their_prefix(tmp_path, monkeypatch, turns):
    paths = RuntimePaths.from_data_dir(tmp_path).session("many-live-threads")
    stores = [ThreadPersistence.open(paths, thread_id=f"thread-{i}") for i in range(12)]
    reads = []
    original_open = Path.open
    original_os_open = os.open
    original_write = os.write
    descriptors = {}
    written_bytes = 0

    def observe_open(path, mode="r", *args, **kwargs):
        if path.name == "messages.jsonl" and "r" in mode:
            reads.append((path, path.stat().st_size))
        return original_open(path, mode, *args, **kwargs)

    def observe_descriptor(path, flags, *args, **kwargs):
        descriptor = original_os_open(path, flags, *args, **kwargs)
        is_trajectory = Path(path).name == "messages.jsonl"
        descriptors[descriptor] = is_trajectory
        if is_trajectory:
            assert flags & os.O_APPEND
        return descriptor

    def observe_write(descriptor, payload):
        nonlocal written_bytes
        count = original_write(descriptor, payload)
        if descriptors.get(descriptor):
            written_bytes += count
        return count

    monkeypatch.setattr(Path, "open", observe_open)
    monkeypatch.setattr(os, "open", observe_descriptor)
    monkeypatch.setattr(os, "write", observe_write)
    for turn in range(turns):
        for store in stores:
            message = _human(turn)
            store.history.append((message,))
            assert store.history.page_transcript(limit=1).items == (message,)
            assert store.history.count_turns() == turn + 1
            reader = ThreadPersistence.open(paths, thread_id=store.thread_id)
            assert reader.history.page_transcript(limit=1).items == (message,)
    # All writes came through these live owners. Paging another session must
    # not evict their known prefixes and make each next append read them again.
    assert reads == []
    assert written_bytes == sum(store.history.path.stat().st_size for store in stores)
    for store in stores:
        assert store.history.count() == turns


def test_reader_folds_only_external_append_suffix(tmp_path, monkeypatch):
    import XBotv2.persistence.store as storage

    persistence = _store(tmp_path)
    original = tuple(_human(index) for index in range(10))
    persistence.history.append(original)
    assert persistence.history.load_surface() == original
    assert persistence.history.load_transcript() == list(original)
    assert len(persistence.history.page_trajectory(limit=100).page.items) == 10

    # Model another process: it uses the production store API but owns a
    # distinct in-memory trajectory state for the same append-only file.
    writer = ThreadPersistence.open(persistence.paths, thread_id="agent")
    writer.history._state = storage._TrajectoryState(writer.history.path)
    assert writer.history.load_surface() == original

    read_bytes = 0
    path_open = Path.open

    class MeasuredStream:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal read_bytes
            line = next(self._stream)
            read_bytes += len(line if isinstance(line, bytes) else line.encode("utf-8"))
            return line

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def measure_open(path, *args, **kwargs):
        stream = path_open(path, *args, **kwargs)
        if path == persistence.history.path:
            return MeasuredStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", measure_open)
    prefix_size = persistence.history.path.stat().st_size
    summary = CompactionSummaryMessage(id=MessageId("external-summary"), summary="context")
    writer.history.replace_surface(
        tuple(message.id for message in original[:5]),
        (summary,),
        operation="compact",
        preserve_transcript=True,
    )
    additions = tuple(_human(index) for index in range(10, 20))
    for message in additions:
        writer.history.append((message,))
        # Exercise all public reader projections after every foreign append.
        persistence.history.load_surface()
        persistence.history.load_transcript()
        persistence.history.page_trajectory(limit=4)

    assert persistence.history.load_surface() == (summary, *original[5:], *additions)
    assert persistence.history.load_transcript() == [*original, *additions]
    trajectory = persistence.history.page_trajectory(limit=100)
    assert trajectory.newest_position == 21
    assert read_bytes == persistence.history.path.stat().st_size - prefix_size


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
    assert history.count_turns() == 3


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


@pytest.mark.parametrize("prefix_size", [10, 100])
def test_compaction_folds_only_the_new_record_on_warm_projections(tmp_path, monkeypatch, prefix_size):
    import XBotv2.persistence.store as storage

    history = _store(tmp_path).history
    original = tuple(_human(i) for i in range(prefix_size))
    history.append(original)
    assert history.load_transcript() == list(original)
    folded = 0
    apply_records = storage._apply_records

    def observe_folding(projection, records):
        nonlocal folded
        folded += len(records)
        return apply_records(projection, records)

    monkeypatch.setattr(storage, "_apply_records", observe_folding)
    source_ids = tuple(message.id for message in original)
    for index in range(3):
        summary = CompactionSummaryMessage(id=MessageId(f"summary-{index}"), summary="context")
        history.replace_surface(source_ids, (summary,), operation="compact", preserve_transcript=True)
        source_ids = (summary.id,)
    assert history.load_surface() == (summary,)
    assert history.load_transcript() == list(original)
    assert folded <= 6  # At most one new record per projection per compact.
    # Nested summaries must still refer to their original transcript span.
    replacement = _human(prefix_size, "cleared")
    history.replace_surface(source_ids, (replacement,), operation="clear", preserve_transcript=False)
    assert history.load_transcript() == [replacement]


@pytest.mark.parametrize("failure", ["invalid", "write", "partial"])
def test_failed_replacement_keeps_both_projections_and_allows_retry(tmp_path, monkeypatch, failure):
    history = _store(tmp_path).history
    original = (_human(1), _human(2))
    history.append(original)
    assert history.load_transcript() == list(original)
    before = history.path.read_bytes()
    summary = CompactionSummaryMessage(id=MessageId("retry-summary"), summary="context")

    original_write = os.write
    partial_written = False

    def reject_write(descriptor, payload):
        nonlocal partial_written
        if failure == "partial" and not partial_written:
            partial_written = True
            return original_write(descriptor, payload[:len(payload) // 2])
        raise OSError("write failed")

    with monkeypatch.context() as patch:
        if failure != "invalid":
            patch.setattr(os, "write", reject_write)
        with pytest.raises((ValueError, OSError)):
            history.replace_surface(
                tuple(message.id for message in original),
                (summary,) if failure != "invalid" else (summary, _human(3)),
                operation="compact", preserve_transcript=True,
            )
    assert history.path.read_bytes() == before
    assert history.load_surface() == original
    assert history.load_transcript() == list(original)
    history.replace_surface(tuple(message.id for message in original), (summary,),
                            operation="compact", preserve_transcript=True)
    assert history.load_surface() == (summary,)
    assert history.load_transcript() == list(original)


def test_corrupt_compaction_never_publishes_a_partially_replayed_view(tmp_path):
    history = _store(tmp_path).history
    original = _human(1)
    history.append((original,))
    summary = CompactionSummaryMessage(id=MessageId("summary-loop"), summary="context")
    history.replace_surface((original.id,), (summary,), operation="compact", preserve_transcript=True)
    valid = StoredTrajectoryRecord(entry=MessageAppended(
        position=3,
        message=_human(2),
    ))
    corrupt = StoredTrajectoryRecord(entry=SurfaceReplaced(
        position=4,
        operation="compact",
        transcript_policy="preserve",
        source_ids=(MessageId("missing-source"),),
        replacements=(CompactionSummaryMessage(
            id=MessageId("summary-loop-2"), summary="context",
        ),),
    ))
    with history.path.open("a", encoding="utf-8") as stream:
        stream.write(valid.model_dump_json() + "\n")
        stream.write(corrupt.model_dump_json() + "\n")

    for read in (history.load_surface, history.load_surface, history.load_transcript):
        with pytest.raises(ValueError, match="not current"):
            read()


def test_trajectory_only_reader_validates_external_projection_suffix(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1),))
    reader = _independent_reopen(persistence)
    assert reader.history.page_trajectory(limit=10).newest_position == 1

    corrupt = StoredTrajectoryRecord(entry=SurfaceReplaced(
        position=2,
        operation="replace",
        transcript_policy="replace",
        source_ids=(MessageId("missing-source"),),
        replacements=(_human(2),),
    ))
    with persistence.history.path.open("a", encoding="utf-8") as stream:
        stream.write(corrupt.model_dump_json() + "\n")

    for _ in range(2):
        with pytest.raises(ValueError, match="not current"):
            reader.history.page_trajectory(limit=10)


def test_cold_trajectory_reader_validates_projection_transitions(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1),))
    corrupt = StoredTrajectoryRecord(entry=SurfaceReplaced(
        position=2,
        operation="replace",
        transcript_policy="replace",
        source_ids=(MessageId("missing-source"),),
        replacements=(_human(2),),
    ))
    with persistence.history.path.open("a", encoding="utf-8") as stream:
        stream.write(corrupt.model_dump_json() + "\n")

    reader = _independent_reopen(persistence)
    with pytest.raises(ValueError, match="not current"):
        reader.history.page_trajectory(limit=10)


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
    # Preserve valid JSONL while changing the selected canonical field.
    path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False) + " "
            for record in records
        ) + "\n",
        encoding="utf-8",
    )


def _independent_reopen(persistence):
    reopened = ThreadPersistence.open(persistence.paths, thread_id="agent")
    reopened.history._state = _TrajectoryState(reopened.history.path)
    return reopened


def test_trajectory_rejects_position_gap_when_reloaded(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1), _human(2)))
    _rewrite_records(
        persistence.history.path,
        lambda records: records[1]["entry"].update(position=9),
    )

    reopened = _independent_reopen(persistence)
    with pytest.raises(ValueError, match="positions must be contiguous"):
        reopened.history.load_surface()


def test_trajectory_rejects_empty_canonical_identity_when_reloaded(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1),))
    _rewrite_records(
        persistence.history.path,
        lambda records: records[0]["entry"]["message"].update(id=""),
    )
    reopened = _independent_reopen(persistence)
    with pytest.raises(ValueError, match="at least 1 character"):
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

    reopened = _independent_reopen(persistence)
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

    reopened = _independent_reopen(persistence)
    with pytest.raises(ValueError, match="reuses a message identity"):
        reopened.history.load_surface()


def test_trajectory_rejects_legacy_flat_record_without_entry_envelope(tmp_path):
    persistence = _store(tmp_path)
    persistence.history.append((_human(1),))
    path = persistence.history.path
    current = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    legacy = {"schema_version": current["schema_version"], **current["entry"]}
    path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

    reopened = _independent_reopen(persistence)
    with pytest.raises(ValueError, match="entry"):
        reopened.history.load_surface()


def test_lifecycle_short_write_preserves_prefix_and_allows_retry(tmp_path, monkeypatch):
    persistence = _store(tmp_path)
    first = ThreadStarted(
        thread_id="child-1",
        parent_thread_id="agent",
        agent="worker",
    )
    second = ThreadFailed(
        thread_id="child-1",
        error="startup failed",
    )
    persistence.lifecycle.append(first)
    before = persistence.paths.session.threads_log.read_bytes()
    raw_write = os.write
    writes = 0

    def short_write(descriptor, payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            return raw_write(descriptor, payload[:-1])
        if writes == 2:
            raise OSError("simulated lifecycle write failure")
        return raw_write(descriptor, payload)

    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(OSError, match="simulated lifecycle write failure"):
        persistence.lifecycle.append(second)
    assert persistence.paths.session.threads_log.read_bytes() == before
    persistence.lifecycle.append(second)

    assert persistence.lifecycle.load() == [first, second]


def test_lifecycle_rejects_the_previous_ambiguous_record_shape(tmp_path):
    persistence = _store(tmp_path)
    persistence.paths.session.threads_log.parent.mkdir(parents=True, exist_ok=True)
    persistence.paths.session.threads_log.write_text(json.dumps({
        "schema_version": 1,
        "event": "completed",
        "thread_id": "child-1",
        "parent_thread_id": "agent",
        "agent": "worker",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "error": "not valid for completion",
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        persistence.lifecycle.load()
