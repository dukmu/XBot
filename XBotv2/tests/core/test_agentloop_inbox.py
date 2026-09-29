"""Behavioral contract for the agent-owned DSH-style inbox."""

import pytest

from XBotv2.agentloop.contracts import (
    Claimed,
    Consumed,
    Edited,
    HumanInput,
    InboxItem,
    Inserted,
    Removed,
    Retargeted,
    RuntimeInput,
)
from XBotv2.core.domain import InboxTarget
from XBotv2.agentloop.events import Events, ObserveInbox
from XBotv2.agentloop.inbox import AgentInbox, EphemeralInboxSink
from XBotv2.persistence.models import StoredInboxRecord


class MemoryInboxSink:
    def __init__(self):
        self.items = []
        self.sizes = []
        self.fail = False

    def append(self, change):
        if self.fail:
            raise OSError("disk full")
        if isinstance(change, Inserted):
            self.items.append(change.item)
        elif isinstance(change, Edited):
            self.items = [item.model_copy(update={"input": item.input.model_copy(update={"content": change.content})})
                          if item.id == change.id else item for item in self.items]
        elif isinstance(change, Retargeted):
            item = next(item for item in self.items if item.id == change.id)
            self.items.remove(item)
            self.items.append(item.model_copy(update={"target": change.target}))
        else:
            ids = (change.id,) if isinstance(change, Removed) else change.ids
            self.items = [item for item in self.items if item.id not in ids]
        self.items.sort(key=lambda item: item.target is InboxTarget.NEXT_TURN)
        self.sizes.append(len(self.items))


class MemoryEvents:
    def __init__(self):
        self.changes = []

    async def emit(self, event, payload):
        assert event == Events.INBOX_CHANGED
        assert isinstance(payload, ObserveInbox)
        self.changes.append(payload.change)


def human_input(input_id, content, target=InboxTarget.NEXT_TURN):
    return InboxItem(
        id=input_id,
        target=target,
        input=HumanInput(content=content),
    )


def test_inbox_records_store_existing_typed_events():
    items = (
        human_input("human", "hello"),
        InboxItem(
            id="runtime",
            target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(
                source="jobs",
                event="completed",
                content="job complete",
            ),
        ),
    )
    for item in items:
        record = StoredInboxRecord(change=Inserted(item=item, wake=False))
        assert StoredInboxRecord.model_validate_json(record.model_dump_json()) == record
    consumed = StoredInboxRecord(change=Consumed(ids=tuple(item.id for item in items)))
    encoded = consumed.model_dump(mode="json")
    assert encoded["change"] == {"kind": "consumed", "ids": ["human", "runtime"]}
    assert StoredInboxRecord.model_validate(encoded) == consumed


@pytest.mark.asyncio
async def test_typed_inputs_share_two_fifo_targets_and_wakeup_semantics():
    events = MemoryEvents()
    sink = MemoryInboxSink()
    inbox = AgentInbox(events=events, sink=sink)
    await inbox.submit(InboxItem(
        id="notice",
        target=InboxTarget.NEXT_STEP,
        input=RuntimeInput(source="jobs", event="completed", content="notice"),
    ), wake=False)
    await inbox.submit(human_input("steer", "correction", InboxTarget.NEXT_STEP), wake=True)
    await inbox.submit(human_input("followup", "question"), wake=True)

    assert [change.wake for change in events.changes[:3] if isinstance(change, Inserted)] == [
        False, True, True,
    ]
    assert sink.sizes[:3] == [1, 2, 3]

    claimed = await inbox.claim_turn()
    assert [item.id for item in claimed] == ["notice", "steer", "followup"]
    assert isinstance(events.changes[-1], Claimed)
    assert events.changes[-1].ids == ("notice", "steer", "followup")
    assert len(inbox) == 0
    assert len(sink.items) == 3

    await inbox.commit([item.id for item in claimed])
    assert isinstance(events.changes[-1], Consumed)
    assert events.changes[-1].ids == ("notice", "steer", "followup")
    assert sink.items == []


@pytest.mark.asyncio
async def test_uncommitted_claim_is_pending_after_restore():
    sink = MemoryInboxSink()
    inbox = AgentInbox(events=MemoryEvents(), sink=sink)
    await inbox.submit(human_input("first", "first"), wake=True)
    await inbox.claim_turn()

    restored = AgentInbox(events=MemoryEvents(), items=sink.items, sink=sink)

    assert [item.id for item in restored.pending] == ["first"]
    assert (await restored.claim_turn())[0].input.content == "first"


@pytest.mark.asyncio
async def test_item_ids_are_unique_across_both_targets():
    inbox = AgentInbox(events=MemoryEvents(), sink=EphemeralInboxSink())
    await inbox.submit(human_input("same", "one"), wake=True)

    with pytest.raises(ValueError, match="Duplicate inbox input id"):
        await inbox.submit(
            human_input("same", "two", InboxTarget.NEXT_STEP),
            wake=True,
        )


@pytest.mark.asyncio
async def test_failed_sink_write_does_not_change_pending_or_claimed_state():
    failed_sink = MemoryInboxSink()
    failed_sink.fail = True
    inbox = AgentInbox(events=MemoryEvents(), sink=failed_sink)

    with pytest.raises(OSError, match="disk full"):
        await inbox.submit(human_input("failed", "not durable"), wake=True)

    assert inbox.pending == []
    assert len(inbox) == 0

    sink = MemoryInboxSink()
    events = MemoryEvents()
    inbox = AgentInbox(events=events, sink=sink)
    await inbox.submit(human_input("durable", "durable"), wake=True)
    claimed = await inbox.claim_turn()
    sink.fail = True

    with pytest.raises(OSError, match="disk full"):
        await inbox.commit([claimed[0].id])

    assert not any(isinstance(change, Consumed) for change in events.changes)
    assert len(inbox) == 0
    assert sink.items[0].id == "durable"
    sink.fail = False
    restored = AgentInbox(events=MemoryEvents(), items=sink.items, sink=sink)
    assert [item.id for item in restored.pending] == ["durable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["turn", "step"])
async def test_failed_claim_observer_does_not_strand_input(boundary):
    class FailingObserver(MemoryEvents):
        fail = True

        async def emit(self, event, payload):
            if self.fail and isinstance(payload.change, Claimed):
                raise RuntimeError("claim observer failed")
            await super().emit(event, payload)

    events = FailingObserver()
    sink = MemoryInboxSink()
    inbox = AgentInbox(events=events, sink=sink)
    item = human_input("retry", "hello", InboxTarget.NEXT_STEP)
    await inbox.submit(item, wake=False)
    claim = inbox.claim_turn if boundary == "turn" else inbox.claim_step
    with pytest.raises(RuntimeError, match="claim observer failed"):
        await claim()
    assert inbox.pending == [item]
    assert sink.items == [item]
    events.fail = False
    assert await claim() == [item]
    await inbox.commit([item.id])
    assert sink.items == []


@pytest.mark.asyncio
async def test_consumption_is_committed_before_observers_run():
    sink = MemoryInboxSink()

    class FailingObserver(MemoryEvents):
        async def emit(self, event, payload):
            if isinstance(payload.change, Consumed):
                assert sink.items == []
                raise RuntimeError("observer failed after commit")
            await super().emit(event, payload)

    inbox = AgentInbox(events=FailingObserver(), sink=sink)
    await inbox.submit(human_input("accepted", "hello"), wake=False)
    await inbox.claim_turn()
    with pytest.raises(RuntimeError, match="observer failed after commit"):
        await inbox.commit(["accepted"])
    assert await inbox.claim_turn() == []
    assert AgentInbox(events=MemoryEvents(), items=sink.items, sink=sink).pending == []


@pytest.mark.asyncio
async def test_pending_input_mutations_keep_sink_and_live_queue_consistent():
    sink = MemoryInboxSink()
    events = MemoryEvents()
    inbox = AgentInbox(events=events, sink=sink)
    await inbox.submit(human_input("edit-me", "draft"), wake=True)
    await inbox.submit(human_input("remove-me", "remove"), wake=True)

    edited = await inbox.edit("edit-me", "edited")
    retargeted = await inbox.retarget("edit-me", InboxTarget.NEXT_STEP)
    removed = await inbox.remove("remove-me")

    assert edited.input.content == "edited"
    assert retargeted.target is InboxTarget.NEXT_STEP
    assert removed.id == "remove-me"
    assert [(item.id, item.input.content, item.target) for item in inbox.pending] == [
        ("edit-me", "edited", InboxTarget.NEXT_STEP),
    ]
    assert [(item.id, item.input.content, item.target) for item in sink.items] == [
        ("edit-me", "edited", InboxTarget.NEXT_STEP),
    ]
    assert events.changes[-3:] == [
        Edited(id="edit-me", content="edited"),
        Retargeted(id="edit-me", target=InboxTarget.NEXT_STEP),
        Removed(id="remove-me"),
    ]


@pytest.mark.asyncio
async def test_claimed_or_unknown_input_cannot_be_mutated():
    inbox = AgentInbox(events=MemoryEvents(), sink=EphemeralInboxSink())
    await inbox.submit(human_input("claimed", "claimed"), wake=True)
    await inbox.claim_turn()

    for operation in (
        lambda: inbox.edit("claimed", "changed"),
        lambda: inbox.remove("claimed"),
        lambda: inbox.retarget("missing", InboxTarget.NEXT_STEP),
    ):
        with pytest.raises(KeyError):
            await operation()
