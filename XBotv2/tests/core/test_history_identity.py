"""Canonical message identity survives history and client projection."""

from __future__ import annotations

import pytest

from XBotv2.core.domain import InputId, MessageId
from XBotv2.core.history import ConversationHistory
from XBotv2.core.messages import HumanInputMessage
from XBotv2.core.parts import TextPart
from XBotv2.session.contracts import conversation_replay


def human(message_id: str, content: str) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId(message_id),
        input_id=InputId(f"input-{message_id}"),
        parts=(TextPart(text=content),),
    )


def test_history_page_and_client_projection_keep_canonical_message_identity() -> None:
    history = ConversationHistory((human("m1", "one"), human("m2", "two")))

    page = history.page(limit=1)
    projected = conversation_replay(page.items)

    assert [message.id for message in page.items] == ["m2"]
    assert [(record.id, record.content) for record in projected] == [("m2", "two")]
    assert page.older_cursor is not None


def test_history_rejects_duplicate_message_identity() -> None:
    with pytest.raises(ValueError, match="unique"):
        ConversationHistory((human("same", "one"), human("same", "two")))


def test_history_replacement_preserves_only_retained_message_ids() -> None:
    first, second = human("m1", "one"), human("m2", "two")
    history = ConversationHistory((first, second))

    history.replace_range(
        1,
        2,
        (human("m3", "replacement"),),
        operation="test:replace",
    )

    assert [message.id for message in history.snapshot()] == ["m1", "m3"]
    assert [record.id for record in conversation_replay(history.snapshot())] == ["m1", "m3"]
