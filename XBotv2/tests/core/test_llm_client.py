"""Tests for llm provider message conversion."""

from types import SimpleNamespace

import pytest

from xbotv2.llm.client import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    _anthropic_usage_values,
    anthropic_request_messages,
    provider_messages,
)
from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall
from xbotv2.core.internal_messages import structure_tool_message


def test_generic_openai_messages_do_not_invent_reasoning_extensions():
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall("c1", "shell", {"command": "ls"})],
        additional_kwargs={"reasoning_content": "private reasoning"},
    )
    out = provider_messages([msg])
    assert "reasoning_content" not in out[0]
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


def test_plain_tool_content_stays_in_the_native_tool_role():
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
    assert message.content == "result <data>"


def test_anthropic_marks_cancelled_tool_result_as_error():
    _system, messages = anthropic_request_messages([
        Message(
            role="tool",
            content="User cancelled the request.",
            tool_call_id="call-1",
            status="cancelled",
        ),
    ])

    assert messages[0]["content"][0]["is_error"] is True


def test_anthropic_request_omits_empty_assistant_and_merges_adjacent_user_blocks():
    _system, messages = anthropic_request_messages([
        Message(
            role="assistant",
            tool_calls=[ToolCall("call-1", "sample", {})],
        ),
        Message(role="tool", tool_call_id="call-1", content="result"),
        Message(role="assistant", content=""),
        Message(role="user", content="continue"),
    ])

    assert messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-1", "name": "sample", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "result",
                },
                {"type": "text", "text": "continue"},
            ],
        },
    ]


def test_anthropic_usage_values_preserve_cache_context_tokens():
    assert _anthropic_usage_values(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=700,
        cache_creation_input_tokens=50,
    ) == {
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
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="thinking", thinking="", signature=""
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="check"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="signature_delta", signature="signed"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="tool_use", id="call-1", name="filesystem_read"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(
                type="input_json_delta", partial_json='{"path":"notes.md"}'
            ),
        ),
        SimpleNamespace(type="content_block_stop", index=1),
        SimpleNamespace(type="message_delta", delta=None, usage=None),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
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
    provider.max_output_tokens = 100
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
    assert final.content == ""
    assert final.tool_calls == [
        ToolCall("call-1", "filesystem_read", {"path": "notes.md"})
    ]
    assert final.additional_kwargs == {
        "reasoning_content": "check",
        "anthropic_content": [
            {"type": "thinking", "thinking": "check", "signature": "signed"},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "filesystem_read",
                "input": {"path": "notes.md"},
            },
        ],
    }
    assert final.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
        "context_tokens": 30,
        "cache_read_input_tokens": 20,
    }
    assert final.response_metadata["stop_reason"] == "tool_use"

    replay_messages = [
        Message(
            role="assistant",
            content=final.content,
            tool_calls=final.tool_calls,
            additional_kwargs=final.additional_kwargs,
            response_metadata=final.response_metadata,
        ),
        Message(role="tool", tool_call_id="call-1", content="file content"),
    ]
    _system, replay = anthropic_request_messages(replay_messages, model="model")
    assert replay[0]["content"] == final.additional_kwargs["anthropic_content"]
    assert replay[1]["content"] == [{
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "file content",
    }]
    _system, switched = anthropic_request_messages(
        replay_messages, model="another-model"
    )
    assert switched[0]["content"] == [{
        "type": "tool_use",
        "id": "call-1",
        "name": "filesystem_read",
        "input": {"path": "notes.md"},
    }]


@pytest.mark.asyncio
async def test_openai_stream_reconstructs_reasoning_tools_and_usage():
    def chunk(*, content=None, reasoning=None, tool_calls=None, usage=None):
        choices = [] if usage else [SimpleNamespace(
            delta=SimpleNamespace(
                content=content,
                reasoning_content=reasoning,
                tool_calls=tool_calls or [],
            ),
            finish_reason="tool_calls" if tool_calls else None,
        )]
        return SimpleNamespace(choices=choices, usage=usage)

    events = [
        chunk(reasoning="check"),
        chunk(content="done", tool_calls=[SimpleNamespace(
            index=0,
            id="call-1",
            function=SimpleNamespace(
                name="filesystem_read",
                arguments='{"path":',
            ),
        )]),
        chunk(tool_calls=[SimpleNamespace(
            index=0,
            id=None,
            function=SimpleNamespace(name=None, arguments='"notes.md"}'),
        )]),
        chunk(usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=3,
            total_tokens=15,
            prompt_cache_hit_tokens=8,
        )),
    ]

    class FakeResponse:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not events:
                raise StopAsyncIteration
            return events.pop(0)

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    provider.model = "model"
    provider.temperature = 0.2
    provider.max_output_tokens = None
    provider.reasoning_effort = "high"
    provider.thinking_enabled = True
    provider.bound_tools = [{"type": "function"}]
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    chunks = [chunk async for chunk in provider.astream([
        Message(role="system", content="instructions"),
        Message(role="user", content="work"),
    ])]
    final = chunks[-1]

    assert captured["stream_options"] == {"include_usage": True}
    assert captured["reasoning_effort"] == "high"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "max_tokens" not in captured
    assert final.content == "done"
    assert final.additional_kwargs == {
        "reasoning_content": "check",
        "openai_message": {
            "role": "assistant",
            "content": "done",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "filesystem_read",
                    "arguments": '{"path": "notes.md"}',
                },
            }],
            "reasoning_content": "check",
        },
    }
    assert final.tool_calls == [
        ToolCall("call-1", "filesystem_read", {"path": "notes.md"})
    ]
    assert final.usage_metadata == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "context_tokens": 12,
        "cache_read_input_tokens": 8,
    }

    replay_message = Message(
        role="assistant",
        content=final.content,
        tool_calls=final.tool_calls,
        additional_kwargs=final.additional_kwargs,
        response_metadata=final.response_metadata,
    )
    replay = provider_messages([replay_message], model="model")
    assert replay == [final.additional_kwargs["openai_message"]]
    switched = provider_messages([replay_message], model="another-model")
    assert "reasoning_content" not in switched[0]
