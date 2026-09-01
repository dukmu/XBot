"""Behavioral contract for the agent-owned DSH-style inbox."""

import pytest

from XBotv2.agentloop.inbox import AgentInbox, InboxTarget


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


@pytest.mark.asyncio
async def test_aliases_share_two_fifo_targets_and_wakeup_semantics():
    splices = []
    wakeups = []
    sink = MemoryInboxSink()

    async def record(event):
        splices.append(event)

    inbox = AgentInbox(
        sink=sink,
        record_splice=record,
        wake_driver=lambda: wakeups.append(1),
    )
    injected = await inbox.inject("notice", message_id="notice")
    steered = await inbox.steer("correction", message_id="steer")
    followed = await inbox.followup("question", message_id="followup")

    assert injected.target is InboxTarget.NEXT_STEP
    assert steered.target is InboxTarget.NEXT_STEP
    assert followed.target is InboxTarget.NEXT_TURN
    assert len(wakeups) == 2
    assert sink.sizes[:3] == [1, 2, 3]

    claimed = await inbox.claim_turn()
    assert [item.message_id for item in claimed] == [
        "notice", "steer", "followup",
    ]
    assert len(inbox) == 0
    assert len(sink.items) == 3

    await inbox.commit([item.message_id for item in claimed])

    assert sink.items == []


@pytest.mark.asyncio
async def test_uncommitted_claim_is_pending_after_restore():
    sink = MemoryInboxSink()
    inbox = AgentInbox(sink=sink)
    await inbox.followup("first", message_id="first")
    await inbox.claim_turn()

    restored = AgentInbox(items=sink.items, sink=sink)

    assert [item.message_id for item in restored.pending] == ["first"]
    assert (await restored.claim_turn())[0].content == "first"


@pytest.mark.asyncio
async def test_message_ids_are_unique_across_both_targets():
    inbox = AgentInbox()
    await inbox.followup("one", message_id="same")

    with pytest.raises(ValueError, match="Duplicate inbox message id"):
        await inbox.steer("two", message_id="same")


@pytest.mark.asyncio
async def test_failed_sink_write_does_not_change_pending_or_claimed_state():
    failed_sink = MemoryInboxSink()
    failed_sink.fail = True
    inbox = AgentInbox(sink=failed_sink)

    with pytest.raises(OSError, match="disk full"):
        await inbox.followup("not durable", message_id="failed")

    assert inbox.pending == []
    assert len(inbox) == 0

    sink = MemoryInboxSink()
    inbox = AgentInbox(sink=sink)
    await inbox.followup("durable", message_id="durable")
    claimed = await inbox.claim_turn()
    sink.fail = True

    with pytest.raises(OSError, match="disk full"):
        await inbox.commit([claimed[0].message_id])

    assert len(inbox) == 0
    assert sink.items[0].message_id == "durable"
    sink.fail = False
    restored = AgentInbox(items=sink.items, sink=sink)
    assert [item.message_id for item in restored.pending] == ["durable"]


@pytest.mark.asyncio
async def test_pending_input_mutations_replace_the_authoritative_snapshot():
    sink = MemoryInboxSink()
    splices = []

    async def record(event):
        splices.append(event)

    inbox = AgentInbox(sink=sink, record_splice=record)
    await inbox.followup("draft", message_id="edit-me")
    await inbox.followup("remove", message_id="remove-me")

    edited = await inbox.edit("edit-me", "edited")
    steered = await inbox.retarget("edit-me", InboxTarget.NEXT_STEP)
    removed = await inbox.remove("remove-me")

    assert edited.content == "edited"
    assert steered.target is InboxTarget.NEXT_STEP
    assert removed.message_id == "remove-me"
    assert [(item.message_id, item.content, item.target) for item in inbox.pending] == [
        ("edit-me", "edited", InboxTarget.NEXT_STEP),
    ]
    assert [(item.message_id, item.content, item.target) for item in sink.items] == [
        ("edit-me", "edited", InboxTarget.NEXT_STEP),
    ]
    assert [event["data"]["operation"] for event in splices] == [
        "insert", "insert", "edit", "retarget", "remove",
    ]


@pytest.mark.asyncio
async def test_claimed_or_unknown_input_cannot_be_mutated():
    inbox = AgentInbox()
    await inbox.followup("claimed", message_id="claimed")
    await inbox.claim_turn()

    for operation in (
        lambda: inbox.edit("claimed", "changed"),
        lambda: inbox.remove("claimed"),
        lambda: inbox.retarget("missing", InboxTarget.NEXT_STEP),
    ):
        with pytest.raises(KeyError):
            await operation()
