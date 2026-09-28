"""Canonical message identity survives history and client projection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from XBotv2.core.domain import MessageId
from XBotv2.core.history import ConversationHistory
from XBotv2.core.messages import HumanInputMessage, RuntimeNoticeMessage
from XBotv2.core.messages import CompactionSummaryMessage
from XBotv2.core.parts import TextPart
from XBotv2.session.contracts import conversation_replay


def human(message_id: str, content: str) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId(message_id),
        parts=(TextPart(text=content),),
    )


@pytest.mark.parametrize("model,payload", [
    (HumanInputMessage, {"id": "accepted-input", "parts": []}),
    (RuntimeNoticeMessage, {"id": "accepted-input", "source": "job", "event": "done", "parts": []}),
])
def test_accepted_message_has_one_canonical_identity(model, payload):
    message = model.model_validate(payload)
    record = message.model_dump(mode="json")
    assert record["id"] == "accepted-input"
    assert "input_id" not in record
    assert "notice_id" not in record
    assert model.model_validate_json(message.model_dump_json()) == message
    old_field = "input_id" if model is HumanInputMessage else "notice_id"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({**payload, old_field: "accepted-input"})


@pytest.mark.parametrize("model,payload,field", [
    (HumanInputMessage, {"id": "m", "parts": []}, "id"),
    (RuntimeNoticeMessage, {"id": "m", "source": "test", "event": "ready", "parts": []}, "id"),
    (CompactionSummaryMessage, {"id": "m", "summary": "context"}, "id"),
])
def test_canonical_message_rejects_empty_identity(model, payload, field):
    assert model.model_validate(payload).id == "m"
    with pytest.raises(ValidationError) as caught:
        model.model_validate({**payload, field: ""})
    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        ((field,), "string_too_short"),
    ]


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


@pytest.mark.parametrize("turns", [20, 40])
def test_nested_compaction_does_not_copy_preserved_transcript(turns):
    traversed = 0

    class MeasuredTranscript(list):
        def __iter__(self):
            nonlocal traversed
            traversed += len(self)
            return super().__iter__()

    history = ConversationHistory()
    # Instrument data traversal, not wall-clock time or the resulting cache size.
    history._transcript = MeasuredTranscript()
    original = []
    for index in range(turns):
        message = human(f"input-{index}", f"turn {index}")
        original.append(message)
        history.append(message)
        history.replace_range(
            0, len(history),
            (CompactionSummaryMessage(id=MessageId(f"summary-{index}"), summary="context"),),
            operation="compact", preserve_transcript=True,
        )
    assert traversed == 0
    assert history.page_transcript(limit=turns).items == tuple(original)
    history.replace_range(0, len(history), (), operation="clear")
    assert history.page_transcript(limit=1).items == ()
