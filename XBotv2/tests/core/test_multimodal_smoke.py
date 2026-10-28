"""Image references stay logical until provider request compilation."""

import pytest
from XBotv2.core.api.artifacts import ArtifactRef
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.provider import ProviderTool, ProviderUser, ResolvedMediaPart
from XBotv2.llm.impl.anthropic import anthropic_request_messages
from XBotv2.llm.impl.openai import openai_messages


def _image(path: str) -> ResolvedMediaPart:
    return ResolvedMediaPart(
        artifact=ArtifactRef(
            id="media/pixel.png", media_type="image/png", size=3,
        ),
        absolute_path=path,
    )


def test_openai_image_projection_reads_the_resolved_path(tmp_path):
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(b"abc")
    messages = (ProviderUser(parts=(TextPart(text="inspect"), _image(str(image_path)))),)
    projected = openai_messages(messages)
    content = projected[0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("inspect\n\n<attachments>")
    assert f'path="{image_path}"' in content[0]["text"]
    assert content[1]["image_url"]["url"] == "data:image/png;base64,YWJj"


def test_anthropic_tool_image_projection_reads_resolved_path_and_keeps_nesting(
    tmp_path,
):
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(b"abc")
    messages = (ProviderTool(
        call_id="call-1",
        parts=(TextPart(text="image loaded"), _image(str(image_path))),
    ),)
    _system, projected = anthropic_request_messages(messages)
    tool_result = projected[0]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"][1]["type"] == "image"
    assert tool_result["content"][1]["source"]["data"] == "YWJj"


def test_image_projection_fails_at_the_file_boundary_when_resolved_path_is_missing(
    tmp_path,
):
    missing_path = tmp_path / "missing.png"
    with pytest.raises(FileNotFoundError):
        openai_messages((ProviderUser(parts=(_image(str(missing_path)),)),))


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["audio/wav", "video/mp4", "application/pdf"])
async def test_unsupported_media_fails_before_provider_send(tmp_path, media_type):
    from XBotv2.core.api.domain import (
        GenerationSettings, ModelRoute, ResolvedModelSelection, StandardGenerationMode,
    )
    from XBotv2.core.api.provider import ModelRequest
    from XBotv2.llm.impl.mock import MockLLM

    provider = MockLLM([{"content": "unused"}], input_modalities=["text", "image"])
    media = ResolvedMediaPart(
        artifact=ArtifactRef(id="media/item", media_type=media_type),
        absolute_path=str(tmp_path / "item"),
    )
    request = ModelRequest(
        messages=(ProviderUser(parts=(media,)),), tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="mock", model="mock"),
            generation=GenerationSettings(mode=StandardGenerationMode(), max_output_tokens=10),
            context_window=100,
        ),
    )
    with pytest.raises(ValueError, match="does not support|Unsupported media type"):
        _ = [event async for event in provider.astream(request)]
    assert provider.call_count == 0
