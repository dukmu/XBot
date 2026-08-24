from XBotv2.core.messages import Message
from XBotv2.usage.plugin import UsageService


def test_usage_owns_snapshot_without_state_store(tmp_path):
    path = tmp_path / "usage.yaml"
    history = [Message(
        role="assistant",
        content="done",
        usage_metadata={"input_tokens": 10, "output_tokens": 4},
    )]

    usage = UsageService(path)
    usage.initialize(history)
    usage.add({"input_tokens": 3, "output_tokens": 2})

    restored = UsageService(path)
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


def test_usage_records_cache_only_request_and_explicit_zero_context(tmp_path):
    usage = UsageService(tmp_path / "usage.yaml")

    assert usage.add({
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
