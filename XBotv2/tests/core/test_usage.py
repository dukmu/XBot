import pytest
from xcore.state import StateService

from XBotv2.core.messages import Message
from XBotv2.usage.plugin import UsageService
from XBotv2.core.usage import UsageDelta


@pytest.mark.asyncio
async def test_usage_owns_typed_snapshot_in_state_namespace(tmp_path):
    state = StateService(path=tmp_path / "state.json").namespace("usage")
    history = [Message(
        role="assistant",
        content="done",
        usage_metadata={"input_tokens": 10, "output_tokens": 4},
    )]

    usage = UsageService(state)
    await usage.initialize(history)
    await usage.add({"input_tokens": 3, "output_tokens": 2})

    restored = UsageService(state)
    await restored.initialize([])
    assert restored.snapshot() == {
        "input_tokens": 13,
        "output_tokens": 6,
        "total_tokens": 19,
        "requests": 2,
        "context_tokens": 3,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "prompt_cache_write_tokens": 0,
    }


@pytest.mark.asyncio
async def test_usage_records_cache_only_request_and_explicit_zero_context(tmp_path):
    usage = UsageService(
        StateService(path=tmp_path / "state.json").namespace("usage")
    )
    await usage.initialize([])

    assert await usage.add({
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 12,
        "cache_creation_input_tokens": 3,
        "prompt_cache_write_tokens": 2,
        "context_tokens": 0,
        "requests": 0,
    }) is True

    assert usage.snapshot() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 17,
        "requests": 0,
        "context_tokens": 0,
        "cache_read_input_tokens": 12,
        "cache_creation_input_tokens": 3,
        "prompt_cache_write_tokens": 2,
    }


@pytest.mark.asyncio
async def test_zero_token_and_total_only_requests_are_not_dropped(tmp_path):
    state_file = tmp_path / "state.json"
    usage = UsageService(StateService(path=state_file).namespace("usage"))
    await usage.initialize([])

    assert not state_file.exists()
    assert await usage.add({"input_tokens": 0, "output_tokens": 0}) is True
    assert await usage.add({"total_tokens": 9}) is True

    assert usage.snapshot()["requests"] == 2
    assert usage.snapshot()["total_tokens"] == 9


@pytest.mark.parametrize(
    "value, error",
    [
        ({"input_tokens": -1}, "non-negative"),
        ({"requests": True}, "non-negative"),
        ({"provider_tokens": 1}, "Unknown usage fields"),
    ],
)
def test_usage_delta_rejects_invalid_provider_fields(value, error):
    with pytest.raises(ValueError, match=error):
        UsageDelta.from_mapping(value)
