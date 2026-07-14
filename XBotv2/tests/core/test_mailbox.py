"""Core runtime mailbox behavior."""

import json

import pytest

from xbotv2.core.mailbox import MailboxMessage, SessionMailbox


@pytest.mark.asyncio
async def test_mailbox_prioritizes_user_messages_and_keeps_fifo(tmp_path):
    mailbox = SessionMailbox(tmp_path / "mailbox.jsonl")
    general = MailboxMessage.create("general", {"source": "task", "event": "done"})
    first = MailboxMessage.create("user_message", "first")
    second = MailboxMessage.create("user_message", "second")

    await mailbox.put(general)
    await mailbox.put(first)
    await mailbox.put(second)

    assert [await mailbox.get(), await mailbox.get(), await mailbox.get()] == [
        first,
        second,
        general,
    ]


@pytest.mark.asyncio
async def test_mailbox_close_drops_runtime_queue_without_restoring_it(tmp_path):
    audit_path = tmp_path / "mailbox.jsonl"
    mailbox = SessionMailbox(audit_path)
    item = MailboxMessage.create("user_message", "pending", request_id="req-1")
    await mailbox.put(item)

    assert await mailbox.close("client_disconnected") == [item]
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [record["event"] for record in records] == ["enqueued", "dropped"]
    assert records[-1]["reason"] == "client_disconnected"

    replacement = SessionMailbox(audit_path)
    assert replacement.size == 0
    await replacement.close()


def test_mailbox_has_only_user_and_general_kinds():
    with pytest.raises(ValueError, match="user_message or general"):
        MailboxMessage.create("shell_task", "done")  # type: ignore[arg-type]
