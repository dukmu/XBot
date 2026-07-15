"""Tests for llm provider message conversion."""

from xbotv2.llm.client import (
    _strip_reasoning_headers,
    anthropic_usage,
    provider_messages,
)
from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall


def test_strip_reasoning_headers_no_header():
    assert _strip_reasoning_headers("hello world") == "hello world"


def test_strip_reasoning_headers_single_header():
    text = "## Thinking\n\nuser wants to get weather"
    assert _strip_reasoning_headers(text) == "user wants to get weather"


def test_strip_reasoning_headers_chain():
    # Pre-existing chained headers (from old session files
    # 20260609-170727-7449) must collapse to a single block.
    text = "## Thinking\n\n## Thinking\n\n## Thinking\n\nreal content"
    assert _strip_reasoning_headers(text) == "real content"


def test_strip_reasoning_headers_keeps_inline_header():
    # The header is only stripped when it appears at the start of
    # the reasoning block. Mid-text `## Thinking` (rare, but
    # possible from the model itself) is preserved.
    text = "## Thinking\n\nfirst ## Thinking inside"
    assert _strip_reasoning_headers(text) == "first ## Thinking inside"


def test_strip_reasoning_headers_empty():
    assert _strip_reasoning_headers("") == ""


def test_provider_messages_strips_reasoning_header_on_replay():
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall("c1", "shell", {"command": "ls"})],
        additional_kwargs={"reasoning_content": "## Thinking\n\n## Thinking\n\nchain"},
    )
    out = provider_messages([msg])
    assert out[0]["reasoning_content"] == "chain"
    # tool_calls and content must still be preserved.
    assert out[0]["tool_calls"][0]["function"]["name"] == "shell"
    assert out[0]["content"] == ""


def test_anthropic_usage_preserves_cache_context_tokens():
    usage = type("Usage", (), {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 50,
    })()

    assert anthropic_usage(usage) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "context_tokens": 850,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 50,
    }
