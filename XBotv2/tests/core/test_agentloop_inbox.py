"""Behavioral contract for the agent-owned DSH-style inbox."""

import pytest

from XBotv2.agentloop.contracts import (
    Claimed,
    Consumed,
    Edited,
    HumanInput,
    InboxItem,
    InboxTarget,
    Inserted,
    Removed,
    Retargeted,
    RuntimeInput,
)
from XBotv2.agentloop.events import Events, ObserveInbox
from XBotv2.agentloop.inbox import AgentInbox, EphemeralInboxSink
from XBotv2.persistence.models import InboxSnapshot


class MemoryInboxSink:
    def __init__(self):
        self.items = []
        self.sizes = []
        self.fail = False

    def replace(self, items):
        if self.fail:
            raise OSError("disk full")
        self.items = list(items)
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


def test_snapshot_stores_versioned_canonical_inbox_items():
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
    encoded = InboxSnapshot(items=items).model_dump(mode="json")

    assert encoded["version"] == 1
    assert [item["id"] for item in encoded["items"]] == ["human", "runtime"]
    assert encoded["items"][0]["input"]["kind"] == "human"
    assert encoded["items"][1]["input"]["kind"] == "runtime"
    assert "schema_version" not in encoded
    assert InboxSnapshot.model_validate(encoded).items == items


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
    assert len(inbox) == 0
    assert len(sink.items) == 3

    await inbox.commit([item.id for item in claimed])
    assert isinstance(events.changes[-1], Consumed)
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
    inbox = AgentInbox(events=MemoryEvents(), sink=sink)
    await inbox.submit(human_input("durable", "durable"), wake=True)
    claimed = await inbox.claim_turn()
    sink.fail = True

    with pytest.raises(OSError, match="disk full"):
        await inbox.commit([claimed[0].id])

    assert len(inbox) == 0
    assert sink.items[0].id == "durable"
    sink.fail = False
    restored = AgentInbox(events=MemoryEvents(), items=sink.items, sink=sink)
    assert [item.id for item in restored.pending] == ["durable"]


@pytest.mark.asyncio
async def test_pending_input_mutations_replace_the_authoritative_snapshot():
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
    assert [type(change) for change in events.changes[-3:]] == [
        Edited, Retargeted, Removed,
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
