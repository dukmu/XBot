"""Tests for llm provider message conversion."""

from types import SimpleNamespace

import pytest

from xbotv2.llm.client import (
    AnthropicProvider,
    _strip_reasoning_headers,
    anthropic_request_messages,
    anthropic_usage,
    provider_messages,
)
from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall
from xbotv2.core.internal_messages import structure_tool_message


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


def test_provider_messages_moves_all_system_content_before_history():
    out = provider_messages([
        Message(role="system", content="base"),
        Message(role="user", content="hello"),
        Message(role="system", content="goal"),
    ])

    assert out == [
        {"role": "system", "content": "base\n\ngoal"},
        {"role": "user", "content": "hello"},
    ]


def test_anthropic_request_uses_top_level_system_and_groups_tool_results():
    system, messages = anthropic_request_messages([
        Message(role="system", content="base"),
        Message(
            role="assistant",
            tool_calls=[
                ToolCall("c1", "first", {}),
                ToolCall("c2", "second", {}),
            ],
        ),
        Message(role="tool", tool_call_id="c1", content="one"),
        Message(role="tool", tool_call_id="c2", content="two"),
        Message(role="system", content="goal"),
    ])

    assert system == "base\n\ngoal"
    assert messages[1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "one"},
            {"type": "tool_result", "tool_use_id": "c2", "content": "two"},
        ],
    }


def test_structured_tool_content_stays_in_the_native_tool_role():
    message = Message(
        role="tool",
        content="result <data>",
        tool_call_id="call-1",
        status="success",
    )
    structure_tool_message(message, "sample")

    openai = provider_messages([message])
    _system, anthropic = anthropic_request_messages([message])

    assert openai == [{
        "role": "tool",
        "content": message.content,
        "tool_call_id": "call-1",
    }]
    assert anthropic == [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": message.content,
        }],
    }]
    assert "<tool_result" in message.content


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


@pytest.mark.asyncio
async def test_anthropic_raw_stream_tolerates_null_delta_usage():
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                model="model",
                usage=SimpleNamespace(
                    input_tokens=10,
                    cache_read_input_tokens=20,
                    cache_creation_input_tokens=0,
                ),
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="done"),
        ),
        SimpleNamespace(type="message_delta", usage=None),
        SimpleNamespace(
            type="message_delta",
            usage=SimpleNamespace(output_tokens=3),
        ),
    ]

    class FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not events:
                raise StopAsyncIteration
            return events.pop(0)

        async def close(self):
            return None

    captured = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = "model"
    provider.temperature = 0.2
    provider.max_tokens = 100
    provider.reasoning_effort = "high"
    provider.thinking_enabled = True
    provider.bound_tools = []
    provider.client = SimpleNamespace(messages=FakeMessages())

    chunks = [chunk async for chunk in provider.astream([
        Message(role="system", content="instructions"),
        Message(role="user", content="work"),
    ])]
    final = chunks[-1]

    assert "tools" not in captured
    assert captured["system"] == "instructions"
    assert captured["extra_body"] == {
        "reasoning_effort": "high",
        "thinking": {"type": "enabled"},
    }
    assert final.content == "done"
    assert final.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
        "context_tokens": 30,
        "cache_read_input_tokens": 20,
    }
