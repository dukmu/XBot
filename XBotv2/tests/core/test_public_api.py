"""Contract tests for the supported XBotv2 extension surface."""

import json

import pytest
from pydantic import ValidationError

from xbotv2.api import (
    HookAction,
    HookDecision,
    HelloRequest,
    MessageRequest,
    PROTOCOL_VERSION,
    ProtocolFrame,
    ToolCall,
    ToolResult,
    XBotTool,
)
from xbotv2.protocol.frames import frame_from_json
from xbotv2.protocol.http_server import create_app


def test_public_api_exports_core_extension_types():
    assert ToolCall(id="1", name="read").arguments == {}
    assert ToolResult.success("ok").status == "success"
    assert HookDecision(HookAction.DENY, "policy").reason == "policy"
    assert XBotTool is not None


def test_wire_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        HelloRequest.model_validate({"protocol_version": PROTOCOL_VERSION, "unknown": True})


def test_message_request_rejects_blank_content():
    with pytest.raises(ValidationError):
        MessageRequest(content="   ")


def test_frame_parser_rejects_unknown_protocol_version():
    frame = ProtocolFrame(
        protocol_version="xbotv2.v999",
        seq=1,
        direction="server_to_client",
        type="end",
        session_id="s",
        thread_id="agent",
        request_id="r",
    )
    with pytest.raises(ValueError, match="Unsupported protocol"):
        frame_from_json(json.dumps(frame.model_dump()))


def test_openapi_uses_typed_request_contracts():
    schema = create_app(no_plugins=True).openapi()
    paths = schema["paths"]
    assert paths["/hello"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/HelloRequest")
    assert paths["/sessions"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/OpenSessionRequest")
    assert paths["/sessions/{session_id}/commands"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/CommandListResponse")
