"""SSE framing validates the single canonical server-event envelope."""

import json

import pytest

from XBotv2.core.domain import SessionScope
from XBotv2.protocol.models import server_event
from XBotv2.protocol.sse import (
    SseDecodeError,
    SseDecoder,
    SseMessage,
    decode_server_event,
    encode_server_event,
)


def _event():
    return server_event(
        kind="history_updated",
        payload={"operation": "undo", "label": "中文"},
        sequence=7,
        session_id="s1",
        thread_id="agent",
        scope=SessionScope(),
    )


def test_server_event_round_trips_through_sse_with_unicode():
    decoder = SseDecoder()
    message = None
    for line in encode_server_event(_event()).decode("utf-8").splitlines():
        message = decoder.feed(line) or message
    assert message is not None
    assert message.event == "history_updated"
    assert message.event_id == "7"
    assert decode_server_event(message) == _event()


def test_decoder_handles_comments_multiline_data_and_final_flush():
    decoder = SseDecoder()
    assert decoder.feed(": heartbeat") is None
    assert decoder.feed("event: notice") is None
    assert decoder.feed("id: abc") is None
    assert decoder.feed("data: first") is None
    assert decoder.feed("data: second") is None

    assert decoder.finish() == SseMessage(
        event="notice", data="first\nsecond", event_id="abc",
    )


def test_decoder_ignores_null_event_id():
    decoder = SseDecoder()
    decoder.feed("id: bad\x00id")
    decoder.feed("data: value")
    assert decoder.feed("").event_id is None


def test_decode_rejects_invalid_json_and_mismatched_event_name():
    with pytest.raises(SseDecodeError, match="not valid JSON"):
        decode_server_event(SseMessage(event="error", data="not-json", event_id="1"))

    payload = json.dumps(_event().model_dump(mode="json"))
    with pytest.raises(SseDecodeError, match="does not match"):
        decode_server_event(SseMessage(event="wrong", data=payload, event_id="7"))


def test_encoder_rejects_multiline_event_kind():
    malformed = _event().model_copy(update={"kind": "bad\nkind"})
    with pytest.raises(ValueError, match="single line"):
        encode_server_event(malformed)
