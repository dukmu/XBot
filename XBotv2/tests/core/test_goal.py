"""Goal evaluation consumes canonical transcript records and typed verdicts."""

import pytest
from pydantic import TypeAdapter, ValidationError

from XBotv2.core.domain import InputId, MessageId, NoticeId
from XBotv2.core.messages import HumanInputMessage, RuntimeNoticeMessage
from XBotv2.core.parts import TextPart
from XBotv2.goal.evaluator import (
    GoalEvaluationError,
    evaluation_request,
    parse_verdict,
    render_transcript,
)
from XBotv2.goal.models import ActiveGoal, GoalState, Impossible, Met, NotMet


def _human(text: str) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId("message-1"),
        input_id=InputId("input-1"),
        parts=(TextPart(text=text),),
    )


def test_transcript_preserves_message_kind_and_bounds_old_content():
    notice = RuntimeNoticeMessage(
        id=MessageId("notice-message"),
        notice_id=NoticeId("notice-1"),
        source="job",
        event="complete",
        parts=(TextPart(text="verified result"),),
    )
    rendered = render_transcript((_human("x" * 5000), notice), max_chars=300)
    assert rendered.startswith("[earlier conversation omitted]")
    assert "[runtime]" in rendered
    assert "verified result" in rendered


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"verdict":"met","reason":"done"}', Met(reason="done")),
        ('{"verdict":"not_yet_met","reason":"pending"}', NotMet(reason="pending")),
        ('{"verdict":"impossible","reason":"blocked"}', Impossible(reason="blocked")),
    ],
)
def test_verdict_parser_returns_closed_typed_variants(payload, expected):
    assert parse_verdict(payload) == expected


@pytest.mark.parametrize("payload", ["", "not json", '{"verdict":"maybe"}'])
def test_verdict_parser_rejects_missing_or_unknown_semantics(payload):
    with pytest.raises(GoalEvaluationError):
        parse_verdict(payload)


def test_goal_state_is_a_closed_discriminated_union():
    adapter = TypeAdapter(GoalState)
    goal = adapter.validate_python({
        "kind": "active", "condition": "tests pass", "started_at": 1.0,
    })
    assert isinstance(goal, ActiveGoal)
    with pytest.raises(ValidationError):
        adapter.validate_python({
            "kind": "running", "condition": "tests pass", "started_at": 1.0,
        })


def test_evaluation_request_has_no_tool_or_transport_envelope():
    request = evaluation_request("tests pass", "[user]\nrun tests")
    assert [message.role for message in request] == ["system", "user"]
    assert "tests pass" in request[1].parts[0].text
