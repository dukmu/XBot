"""Focused multimodal smoke tests for XBot core paths."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from api.messages import ImageContent, Message
from core.bootstrap import bootstrap
from core.builtin_tools.content import content_read
from core.builtin_tools.filesystem import read_file
from llm.anthropic import anthropic_request_messages
from llm.mock import MockLLM
from llm.openai import openai_messages
from api.paths import RuntimePaths


PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
)


class MediaSandbox:
    def __init__(self, session_root: Path) -> None:
        self.session_root = session_root
        self.enabled = False
        self.network = True

    def resolve_filesystem_args(self, _operation: str, args: dict[str, object]) -> dict[str, object]:
        return args


@pytest.mark.asyncio
async def test_content_read_path_produces_image_tool_result(tmp_path):
    payload = base64.b64decode(PNG_BASE64)
    path = tmp_path / "pixel.png"
    path.write_bytes(payload)
    sandbox = MediaSandbox(tmp_path / "session")

    result = await content_read(path=str(path), sandbox=sandbox)

    assert result.status == "success"
    assert len(result.images) == 1
    image = result.images[0]
    assert image.media_type == "image/png"
    assert (tmp_path / "session" / image.path).read_bytes() == payload

    text_result = await read_file(str(path), sandbox=sandbox)
    assert text_result.status == "success"
    assert text_result.images == ()
    assert "Non-text file" in text_result.content


@pytest.mark.asyncio
async def test_content_read_accepts_base64_and_data_url(tmp_path):
    encoded = PNG_BASE64
    sandbox = MediaSandbox(tmp_path / "session")

    raw = await content_read(data=encoded, sandbox=sandbox)
    data_url = await content_read(
        data=f"data:image/png;base64,{encoded}",
        sandbox=sandbox,
    )

    assert raw.status == "success"
    assert data_url.status == "success"
    assert raw.images[0].media_type == "image/png"
    assert data_url.images[0].size == len(base64.b64decode(encoded))


@pytest.mark.asyncio
async def test_content_read_url_requires_network(tmp_path):
    class OfflineSandbox:
        session_root = tmp_path / "session"
        enabled = False
        network = False

    result = await content_read(
        url="https://example.com/cat.png",
        sandbox=OfflineSandbox(),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "network_disabled"


def test_provider_encoding_preserves_image_content():
    image = ImageContent("artifacts/media/pixel.png", "image/png", 3)

    tool_message = Message(
        role="tool",
        content="image loaded",
        tool_call_id="call-1",
        images=[image],
    )
    _system, anthropic = anthropic_request_messages(
        [tool_message],
        image_loader=lambda _path: "YWJj",
    )
    assert anthropic[0]["content"][0]["type"] == "tool_result"
    assert anthropic[0]["content"][0]["content"][1]["type"] == "image"

    with pytest.raises(ValueError, match="only in user messages"):
        openai_messages([tool_message], image_loader=lambda _path: "YWJj")

    user_message = Message(
        role="user",
        content="describe",
        images=[image],
    )
    openai = openai_messages([user_message], image_loader=lambda _path: "YWJj")
    assert openai[0]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_provider_rejects_image_without_image_modality(tmp_path):
    llm = MockLLM(input_modalities=["text"], media_root=tmp_path)
    image = ImageContent("artifacts/media/pixel.png", "image/png", 3)
    message = Message(role="user", content="describe", images=[image])

    with pytest.raises(ValueError, match="does not support image"):
        async for _ in llm.astream([message]):
            pass


@pytest.mark.asyncio
async def test_engine_smoke_persists_user_image_attachment(
    temp_data_dir,
    temp_workspace,
):
    payload = base64.b64decode(PNG_BASE64)
    llm = MockLLM(
        responses=[{"content": "red square"}],
        input_modalities=["text", "image"],
        media_root=temp_data_dir,
    )
    engine = await bootstrap(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="multimodal-smoke",
        thread_id="agent",
        plugin_dirs=[],
        llm_override=llm,
    )
    engine.sandbox_policy.workspace_root = temp_workspace
    image = engine.state_store.store_image(PNG_BASE64, "image/png")

    events = [
        event
        async for event in engine.run_turn("describe the image", images=[image])
    ]

    assert any(event["type"] == "turn_finished" for event in events)
    user_messages = [message for message in engine.messages if message.role == "user"]
    assert user_messages[-1].images == [image]

    provider_user = next(
        message
        for message in llm.get_call_messages(0)
        if message.role == "user"
    )
    assert provider_user.images == [image]
    assert (engine.state_store.root / image.path).is_file()

    persisted = engine.state_store.messages_path.read_text(encoding="utf-8")
    assert '"type": "image"' in persisted
