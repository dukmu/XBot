"""Tests for the shared SSE wire codec."""

import json

import pytest
from pydantic import ValidationError

from xbotv2.protocol.models import (
    KNOWN_SERVER_EVENT_TYPES,
    PermissionResponseRequest,
    ServerEvent,
    TYPED_SERVER_EVENT_TYPES,
    UserInputResponseRequest,
    server_event,
)
from xbotv2.protocol.sse import (
    SseDecoder,
    SseMessage,
    decode_server_event,
    encode_server_event,
)


def test_encode_server_event_preserves_envelope_and_unicode() -> None:
    event = server_event(
        session_id="s1",
        thread_id="t1",
        request_id="r1",
        sequence=7,
        type="assistant_message",
        data={"content": "你好"},
    )

    encoded = encode_server_event(event).decode("utf-8")

    assert encoded.startswith("event: assistant_message\nid: 7\ndata: ")
    assert encoded.endswith("\n\n")
    payload = json.loads(encoded.split("data: ", 1)[1].strip())
    assert payload == event.model_dump()


def test_every_known_server_event_type_has_a_payload_dto() -> None:
    assert set(TYPED_SERVER_EVENT_TYPES) == set(KNOWN_SERVER_EVENT_TYPES)


def test_decoder_handles_comments_multiline_data_and_text_id() -> None:
    decoder = SseDecoder()

    assert decoder.feed(": keep-alive") is None
    assert decoder.feed("event: message") is None
    assert decoder.feed("id: event-7") is None
    assert decoder.feed("data: first") is None
    assert decoder.feed("data: second") is None
    message = decoder.feed("")

    assert message is not None
    assert message.event == "message"
    assert message.event_id == "event-7"
    assert message.data == "first\nsecond"


def test_decoder_flushes_unterminated_final_message() -> None:
    decoder = SseDecoder()
    decoder.feed("event: end")
    decoder.feed("data: {\"type\":\"end\"}")

    message = decoder.finish()

    assert message is not None
    assert message.event == "end"
    assert message.data == '{"type":"end"}'
    assert decoder.finish() is None


def test_decoder_ignores_null_event_id() -> None:
    decoder = SseDecoder()
    decoder.feed("id: invalid\x00id")
    decoder.feed("data: payload")

    message = decoder.feed("")

    assert message is not None
    assert message.event_id is None


def test_decode_server_event_surfaces_malformed_json() -> None:
    event = decode_server_event(
        SseMessage(event="assistant_message", data="not-json", event_id="7")
    )

    assert event.type == "error"
    assert event.data == {"code": "sse_decode_error", "message": "not-json"}


def test_decode_server_event_surfaces_invalid_interaction_payload() -> None:
    event = decode_server_event(
        SseMessage(
            event="user_input_required",
            event_id="7",
            data=json.dumps({
                "type": "user_input_required",
                "data": {"request_id": "user_input:c1"},
            }),
        )
    )

    assert event.type == "error"
    assert event.data["code"] == "sse_decode_error"
    assert "data.question" in event.data["message"]


def test_user_input_event_preserves_structured_options() -> None:
    event = server_event(
        type="user_input_required",
        data={
            "request_id": "user_input:c1",
            "source": "ask_user",
            "tool_call_id": "c1",
            "question": "Continue?",
            "options": [
                {"label": "continue", "description": "Keep working."},
                {"label": "stop", "description": "Stop now."},
            ],
        },
    )

    assert event.data["options"] == [
        {"label": "continue", "description": "Keep working."},
        {"label": "stop", "description": "Stop now."},
    ]


def test_encoder_rejects_line_breaks_in_event_type() -> None:
    event = server_event(type="message\ninjected", sequence=1)

    with pytest.raises(ValueError, match="single line"):
        encode_server_event(event)


def test_interaction_response_requests_have_distinct_schemas() -> None:
    permission = PermissionResponseRequest(
        request_id="permission:c1",
        decision="allow",
        scope="session",
    )
    user_input = UserInputResponseRequest(
        request_id="user_input:c2",
        answer={"choice": "continue"},
    )

    assert permission.model_dump() == {
        "request_id": "permission:c1",
        "decision": "allow",
        "scope": "session",
    }
    assert user_input.model_dump() == {
        "request_id": "user_input:c2",
        "answer": {"choice": "continue"},
    }

    with pytest.raises(ValidationError):
        PermissionResponseRequest(
            request_id="permission:c1",
            decision="approve",
        )


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        (
            "permission_request",
            {
                "request_id": "permission:c1",
                "source": "permission_system",
                "reason": "Approval: shell",
            },
        ),
        (
            "user_input_required",
            {
                "request_id": "user_input:c2",
                "source": "ask_user",
                "tool_call_id": "c2",
                "options": [],
            },
        ),
    ],
)
def test_server_event_rejects_incomplete_interaction_payloads(
    event_type: str,
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ServerEvent(type=event_type, data=data)


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("error", {"message": "missing code"}),
        (
            "tool_result",
            {"name": "shell", "content": "ok", "status": "success"},
        ),
        (
            "task_updated",
            {"task_id": "task-1", "status": "running"},
        ),
        (
            "usage",
            {
                "input_tokens": -1,
                "output_tokens": 1,
                "total_tokens": 0,
                "requests": 1,
            },
        ),
    ],
)
def test_server_event_rejects_invalid_stable_payloads(
    event_type: str,
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ServerEvent(type=event_type, data=data)


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("assistant_message", {"tool_calls": []}),
        ("assistant_message_delta", {}),
        ("turn_started", {"turn": 0}),
        ("turn_cancelled", {"turn": 1}),
        ("end", {"status": ""}),
    ],
)
def test_server_event_rejects_invalid_turn_and_assistant_payloads(
    event_type: str,
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ServerEvent(type=event_type, data=data)


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        (
            "client_message",
            {"message": "notice", "level": "info", "source": "send_message"},
        ),
        (
            "permission_denied",
            {
                "request_id": "permission:c1",
                "source": "permission_system",
                "tool_call": {},
                "decision": "allow",
                "reason": "denied",
            },
        ),
        ("tool_calls_started", {"tool_calls": []}),
        (
            "tool_call_delta",
            {
                "tool_calls": [{
                    "tool_call_id": "c1",
                    "id": "c1",
                    "name": "shell",
                    "args_delta": "{}",
                    "args": "{}",
                    "index": -1,
                }],
            },
        ),
    ],
)
def test_server_event_rejects_invalid_client_and_tool_call_payloads(
    event_type: str,
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ServerEvent(type=event_type, data=data)
