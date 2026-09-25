"""Provider adapters translate canonical requests without owning conversation state."""

from types import SimpleNamespace

from anthropic.types import (
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
)
from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
import pytest

from XBotv2.core.domain import (
    GenerationSettings,
    MeasurementUnavailable,
    ModelRoute,
    ProviderMeasured,
    ResolvedModelSelection,
    StandardGenerationMode,
    ToolCallId,
)
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.provider import (
    ModelRequest,
    ProviderAssistant,
    ProviderSystem,
    ProviderUser,
    ProviderTool,
    ToolSchema,
)
from XBotv2.core.stream import ModelCompleted, ModelFailed
from XBotv2.core.tools import ToolCall
from XBotv2.llm.anthropic import (
    AnthropicProvider,
    anthropic_request_messages,
    anthropic_tool_schema,
)
from XBotv2.llm.base import provider_usage
from XBotv2.llm.client import ToolArgumentsError, _parse_tool_args
from XBotv2.llm.openai import (
    OpenAICompatibleProvider,
    normalize_openai_usage,
    openai_messages,
    openai_tool_call,
    openai_tool_schema,
)


def test_provider_usage_keeps_delta_and_observation_as_distinct_values():
    delta, observed = provider_usage(
        input_tokens=10,
        output_tokens=3,
        context_tokens=15,
        cache_read_input_tokens=5,
    )
    assert delta.counters.input == 10
    assert delta.counters.cache_read == 5
    assert observed == ProviderMeasured(tokens=15)

    _delta, unavailable = provider_usage(input_tokens=0, output_tokens=0)
    assert isinstance(unavailable, MeasurementUnavailable)


def test_openai_usage_normalizes_typed_sdk_fields_and_cache_counters():
    usage = CompletionUsage.model_validate({
        "prompt_tokens": 20,
        "completion_tokens": 4,
        "total_tokens": 24,
        "prompt_tokens_details": {"cached_tokens": 6},
        "cache_creation_input_tokens": 2,
        "prompt_cache_write_tokens": 3,
    })

    delta, observed = normalize_openai_usage(usage)

    assert delta.counters.input == 12
    assert delta.counters.output == 4
    assert delta.counters.cache_read == 6
    assert delta.counters.cache_create == 2
    assert delta.counters.prompt_cache_write == 3
    assert observed == ProviderMeasured(tokens=20)


def test_openai_usage_rejects_malformed_known_extension_counter():
    usage = CompletionUsage.model_validate({
        "prompt_tokens": 20,
        "completion_tokens": 4,
        "total_tokens": 24,
        "prompt_cache_hit_tokens": "six",
    })

    with pytest.raises(TypeError, match="prompt_cache_hit_tokens"):
        normalize_openai_usage(usage)


def test_openai_projection_preserves_roles_reasoning_and_tool_calls():
    call = ToolCall(id=ToolCallId("call-1"), name="lookup", args={"q": "x"})
    messages = (
        ProviderSystem(parts=(TextPart(text="system"),)),
        ProviderUser(parts=(TextPart(text="question"),)),
        ProviderAssistant(parts=(ReasoningPart(text="think"), call)),
        ProviderTool(call_id="call-1", parts=(TextPart(text="result"),)),
    )
    projected = openai_messages(messages)
    assert [item["role"] for item in projected] == ["system", "user", "assistant", "tool"]
    assert projected[2]["tool_calls"] == [openai_tool_call(call)]
    assert projected[3]["tool_call_id"] == "call-1"


def test_anthropic_projection_separates_system_and_groups_user_tool_messages():
    messages = (
        ProviderSystem(parts=(TextPart(text="system"),)),
        ProviderUser(parts=(TextPart(text="question"),)),
        ProviderTool(call_id="call-1", parts=(TextPart(text="result"),)),
    )
    system, projected = anthropic_request_messages(messages)
    assert system == "system"
    assert len(projected) == 1
    assert projected[0]["role"] == "user"
    assert projected[0]["content"][1]["type"] == "tool_result"


def test_tool_schemas_are_derived_from_one_provider_neutral_schema():
    schema = ToolSchema(
        name="lookup",
        description="Look up data",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    assert openai_tool_schema(schema)["function"]["name"] == "lookup"
    assert anthropic_tool_schema(schema)["name"] == "lookup"


def test_tool_arguments_parser_rejects_non_object_json():
    assert _parse_tool_args('{"q":"x"}', tool_name="lookup") == {"q": "x"}
    with pytest.raises(ToolArgumentsError):
        _parse_tool_args('["x"]', tool_name="lookup")
    with pytest.raises(ToolArgumentsError):
        _parse_tool_args('{bad', tool_name="lookup")


@pytest.mark.asyncio
async def test_openai_stream_completes_with_a_typed_terminal_event():
    """The real adapter must turn a normal provider finish into ModelCompleted."""
    def chunk(delta, *, finish_reason=None):
        return ChatCompletionChunk.model_validate({
            "id": "completion-1",
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
            "created": 1,
            "model": "test-model",
            "object": "chat.completion.chunk",
        })

    class Completions:
        async def create(self, **_kwargs):
            async def chunks():
                yield chunk({"content": "hello"}, finish_reason="stop")

            return chunks()

    provider = OpenAICompatibleProvider(api_key="test", base_url=None)
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hi"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128
            ),
            context_window=4096,
        ),
    )

    events = [event async for event in provider.astream(request)]

    assert isinstance(events[-1], ModelCompleted)
    assert "".join(part.text for part in events[-1].response.parts) == "hello"


@pytest.mark.asyncio
async def test_openai_tool_call_fragments_are_assembled_from_sdk_chunk_types():
    def chunk(tool_call, *, finish_reason=None):
        return ChatCompletionChunk.model_validate({
            "id": "completion-1",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [tool_call]},
                "finish_reason": finish_reason,
            }],
            "created": 1,
            "model": "test-model",
            "object": "chat.completion.chunk",
        })

    class Completions:
        async def create(self, **_kwargs):
            async def chunks():
                yield chunk({
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":'},
                })
                yield chunk({
                    "index": 0,
                    "function": {"arguments": '"x"}'},
                }, finish_reason="tool_calls")

            return chunks()

    provider = OpenAICompatibleProvider(api_key="test", base_url=None)
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hi"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128
            ),
            context_window=4096,
        ),
    )

    events = [event async for event in provider.astream(request)]

    assert isinstance(events[-1], ModelCompleted)
    call = events[-1].response.parts[0]
    assert call.name == "lookup"
    assert call.args == {"q": "x"}


@pytest.mark.asyncio
async def test_openai_does_not_complete_a_tool_call_without_its_name():
    malformed = ChatCompletionChunk.model_validate({
        "id": "completion-1",
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"arguments": "{}"},
            }]},
            "finish_reason": "tool_calls",
        }],
        "created": 1,
        "model": "test-model",
        "object": "chat.completion.chunk",
    })

    class Completions:
        async def create(self, **_kwargs):
            async def chunks():
                yield malformed

            return chunks()

    provider = OpenAICompatibleProvider(api_key="test", base_url=None)
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hi"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128
            ),
            context_window=4096,
        ),
    )

    events = [event async for event in provider.astream(request)]

    assert isinstance(events[-1], ModelFailed)
    assert all(not isinstance(event, ModelCompleted) for event in events)


@pytest.mark.asyncio
async def test_anthropic_stream_completes_with_a_typed_terminal_event():
    class MessageStream:
        async def __aiter__(self):
            yield RawMessageStartEvent.model_validate({
                "type": "message_start",
                "message": {
                    "id": "message-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "test-model",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })
            yield RawContentBlockStartEvent.model_validate({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            yield RawContentBlockDeltaEvent.model_validate({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            })
            yield RawMessageDeltaEvent.model_validate({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            })

        async def close(self):
            pass

    class Messages:
        async def create(self, **_kwargs):
            return MessageStream()

    provider = AnthropicProvider(api_key="test", base_url=None)
    provider.client = SimpleNamespace(messages=Messages())
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hi"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128
            ),
            context_window=4096,
        ),
    )

    events = [event async for event in provider.astream(request)]

    assert isinstance(events[-1], ModelCompleted)
    assert "".join(part.text for part in events[-1].response.parts) == "hello"


@pytest.mark.asyncio
async def test_anthropic_preserves_tool_input_from_content_block_start():
    class MessageStream:
        def __init__(self):
            self.events = [
                RawMessageStartEvent.model_validate({
                    "type": "message_start",
                    "message": {
                        "id": "message-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "test-model",
                        "usage": {"input_tokens": 2, "output_tokens": 0},
                    },
                }),
                RawContentBlockStartEvent.model_validate({
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "lookup",
                        "input": {"q": "from-start"},
                    },
                }),
                RawContentBlockStopEvent.model_validate({
                    "type": "content_block_stop",
                    "index": 0,
                }),
                RawContentBlockStartEvent.model_validate({
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call-2",
                        "name": "lookup",
                        "input": {},
                    },
                }),
                RawContentBlockDeltaEvent.model_validate({
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
                }),
                RawContentBlockDeltaEvent.model_validate({
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '"from-delta"}'},
                }),
                RawContentBlockStopEvent.model_validate({
                    "type": "content_block_stop",
                    "index": 1,
                }),
                RawMessageDeltaEvent.model_validate({
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                }),
            ]

        def __aiter__(self):
            return self._iterate()

        async def _iterate(self):
            for event in self.events:
                yield event

        async def close(self):
            pass

    class Messages:
        async def create(self, **_kwargs):
            return MessageStream()

    provider = AnthropicProvider(api_key="test", base_url=None)
    provider.client = SimpleNamespace(messages=Messages())
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hi"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128
            ),
            context_window=4096,
        ),
    )

    events = [event async for event in provider.astream(request)]

    assert isinstance(events[-1], ModelCompleted)
    calls = events[-1].response.parts
    assert [call.args for call in calls] == [
        {"q": "from-start"},
        {"q": "from-delta"},
    ]


@pytest.mark.asyncio
async def test_anthropic_rejects_delta_without_its_content_block_start():
    class MessageStream:
        async def __aiter__(self):
            yield RawMessageStartEvent.model_validate({
                "type": "message_start",
                "message": {
                    "id": "message-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "test-model",
                    "usage": {"input_tokens": 2, "output_tokens": 0},
                },
            })
            yield RawContentBlockDeltaEvent.model_validate({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "orphan"},
            })
            yield RawMessageDeltaEvent.model_validate({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            })

        async def close(self):
            pass

    class Messages:
        async def create(self, **_kwargs):
            return MessageStream()

    provider = AnthropicProvider(api_key="test", base_url=None)
    provider.client = SimpleNamespace(messages=Messages())
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hi"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128
            ),
            context_window=4096,
        ),
    )

    events = [event async for event in provider.astream(request)]

    assert isinstance(events[-1], ModelFailed)
    assert all(not isinstance(event, ModelCompleted) for event in events)
