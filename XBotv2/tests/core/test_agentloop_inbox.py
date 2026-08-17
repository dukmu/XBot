"""Behavioral contract for the agent-owned DSH-style inbox."""

import pytest

from XBotv2.agentloop.inbox import AgentInbox, InboxTarget


@pytest.mark.asyncio
async def test_aliases_share_two_fifo_targets_and_wakeup_semantics():
    splices = []
    wakeups = []
    inbox = None

    async def record(event):
        # The durable splice is observed before the live projection mutates.
        splices.append((event, len(inbox)))

    inbox = AgentInbox(record_splice=record, wake_driver=lambda: wakeups.append(1))
    injected = await inbox.inject("notice", message_id="notice")
    steered = await inbox.steer("correction", message_id="steer")
    followed = await inbox.followup("question", message_id="followup")

    assert injected.target is InboxTarget.NEXT_STEP
    assert steered.target is InboxTarget.NEXT_STEP
    assert followed.target is InboxTarget.NEXT_TURN
    assert len(wakeups) == 2
    assert [size for _, size in splices[:3]] == [0, 1, 2]

    claimed = await inbox.claim_turn()
    assert [item.message_id for item in claimed] == [
        "notice", "steer", "followup",
    ]
    assert len(inbox) == 0


@pytest.mark.asyncio
async def test_splice_replay_restores_only_unclaimed_input():
    events = []

    async def record(event):
        events.append(event)

    inbox = AgentInbox(record_splice=record)
    await inbox.followup("first", message_id="first")
    await inbox.claim_turn()
    await inbox.inject("pending", message_id="pending", source="job")

    restored = AgentInbox()
    restored.restore(events)

    assert [item.message_id for item in restored.pending] == ["pending"]
    assert (await restored.claim_step())[0].content == "pending"


@pytest.mark.asyncio
async def test_message_ids_are_unique_across_both_targets():
    inbox = AgentInbox()
    await inbox.followup("one", message_id="same")

    with pytest.raises(ValueError, match="Duplicate inbox message id"):
        await inbox.steer("two", message_id="same")
