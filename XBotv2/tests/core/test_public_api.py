"""Contract tests for the supported XBotv2 extension surface."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import xbotv2.api as public_api

from xbotv2.api import (
    Command,
    CommandResult,
    ContextComponent,
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
    PluginConfigError,
    PluginManifest,
    PromptFragmentStage,
    RuntimePaths,
    RuntimePluginContext,
    SessionPaths,
    ToolCall,
    ToolRegistrationOptions,
    ToolResult,
    Tool,
)
from xbotv2.protocol.version import PROTOCOL_VERSION
from xbotv2.protocol.http_server import create_app
from xbotv2.protocol.models import (
    KNOWN_SERVER_EVENT_TYPES,
    HelloRequest,
    MessageRequest,
    server_event,
)


def test_public_api_inventory_is_explicit():
    inventory = Path(__file__).parents[2] / "docsv2" / "api_inventory.md"
    documented = [
        match.group(1)
        for line in inventory.read_text(encoding="utf-8").splitlines()
        if (match := re.match(r"^\| `([^`]+)` \|", line))
    ]

    assert documented == public_api.__all__
    assert len(documented) == len(set(documented))
    assert all(hasattr(public_api, name) for name in documented)


def test_public_api_exports_core_extension_types():
    assert ToolCall(id="1", name="read").args == {}
    assert ContextComponent(
        role="system",
        source="plugin",
        content="instructions",
        plugin_name="sample",
        stage="system_instructions",
    ).stage == "system_instructions"
    assert ToolResult.success("ok").status == "success"
    assert HookDecision(HookAction.DENY, "policy").reason == "policy"
    assert Tool is not None
    assert Command(name="sample", description="Sample", handler=lambda *_: None).name == "sample"
    assert CommandResult("done").status == "ok"
    assert RuntimePaths is not None
    assert RuntimePluginContext is not None
    assert PromptFragmentStage is not None
    assert hasattr(RuntimePluginContext, "register_tool")
    assert hasattr(RuntimePluginContext, "unregister_tool")
    assert hasattr(RuntimePluginContext, "register_command")
    assert hasattr(RuntimePluginContext, "unregister_command")
    assert SessionPaths is not None
    error = PluginConfigError("sample", ("limits", 0), "invalid")
    assert error.path == ("limits", 0)
    assert HookContext(
        stage=HookStage.ON_TURN_START,
        request_id="request-1",
    ).request_id == "request-1"
    assert HookContext(stage=HookStage.BEFORE_CONTEXT).invoke_model is None
    assert HookContext(stage=HookStage.ON_SESSION_INIT).request_user_input is None


def test_tool_registration_options_validate_values():
    options = ToolRegistrationOptions(
        sandbox_mode="sandboxed",
        namespace="plugin:test",
    )

    assert options.sandbox_mode == "sandboxed"
    assert options.namespace == "plugin:test"

    with pytest.raises(ValueError, match="sandbox_mode"):
        ToolRegistrationOptions(sandbox_mode="invalid")

    with pytest.raises(TypeError, match="execution_mode"):
        ToolRegistrationOptions(execution_mode="parallel")


def test_command_contract_separates_server_handlers_from_prompt_metadata():
    async def handler(_ctx, _raw_args):
        return CommandResult("ok")

    assert Command(
        name="server-command",
        description="Server command",
        handler=handler,
    ).kind == "server"
    assert Command(
        name="prompt-command",
        description="Prompt command",
        kind="prompt",
    ).handler is None

    with pytest.raises(ValueError, match="requires a handler"):
        Command(name="missing", description="Missing")
    with pytest.raises(ValueError, match="must not define a handler"):
        Command(name="prompt", description="Prompt", kind="prompt", handler=handler)
    with pytest.raises(ValueError, match="lowercase"):
        Command(name="/Invalid", description="Invalid", handler=handler)


def test_plugin_manifest_rejects_unimplemented_tool_scheduling_metadata():
    with pytest.raises(ValidationError, match="execution_mode"):
        PluginManifest(
            name="sample",
            version="1",
            tools=[{"handler": "sample:tool", "execution_mode": "parallel"}],
        )


@pytest.mark.parametrize(
    "stage",
    [
        "system_prefix",
        "system_instructions",
        "system_rules",
        "context_suffix",
    ],
)
def test_plugin_manifest_accepts_supported_prompt_fragment_stages(stage):
    manifest = PluginManifest(
        name="sample",
        version="1",
        prompt_fragments=[{"stage": stage, "handler": "sample:render"}],
    )

    assert manifest.prompt_fragments[0].stage == stage


def test_plugin_manifest_rejects_legacy_dag_suffix_stage():
    with pytest.raises(ValidationError, match="dag_suffix"):
        PluginManifest(
            name="sample",
            version="1",
            prompt_fragments=[
                {"stage": "dag_suffix", "handler": "sample:render"}
            ],
        )


@pytest.mark.parametrize(
    "fragment",
    [
        {"stage": "system_instructions"},
        {
            "stage": "system_instructions",
            "file": "prompt.md",
            "handler": "sample:render",
        },
        {"stage": "system_instructions", "file": ""},
        {
            "stage": "system_instructions",
            "handler": "sample:render",
            "unknown": True,
        },
    ],
)
def test_plugin_manifest_requires_one_prompt_fragment_source(fragment):
    with pytest.raises(ValidationError):
        PluginManifest(
            name="sample",
            version="1",
            prompt_fragments=[fragment],
        )


def test_wire_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        HelloRequest.model_validate({"protocol_version": PROTOCOL_VERSION, "unknown": True})


def test_message_request_rejects_blank_content():
    with pytest.raises(ValidationError):
        MessageRequest(content="   ")


def test_server_event_carries_stream_envelope_fields():
    event = server_event(
        session_id="s1",
        thread_id="t1",
        request_id="req-1",
        sequence=7,
        type="assistant_message",
        data={"content": "ok"},
    )

    assert event.protocol_version == PROTOCOL_VERSION
    assert event.session_id == "s1"
    assert event.thread_id == "t1"
    assert event.request_id == "req-1"
    assert event.sequence == 7
    assert event.type == "assistant_message"
    assert event.data == {"content": "ok"}


def test_server_event_type_inventory_covers_current_stream_events():
    assert set(KNOWN_SERVER_EVENT_TYPES) == {
        "assistant_message",
        "assistant_message_delta",
        "client_message",
        "end",
        "error",
        "message_queued",
        "permission_denied",
        "permission_request",
        "permission_response_recorded",
        "tool_call_delta",
        "tool_calls_started",
        "tool_result",
        "task_updated",
        "turn_cancelled",
        "turn_finished",
        "turn_started",
        "usage",
        "user_input_recorded",
        "user_input_required",
    }


def test_openapi_uses_typed_request_contracts():
    schema = create_app(paths=RuntimePaths.from_data_dir("data"), no_plugins=True).openapi()
    paths = schema["paths"]
    assert paths["/hello"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/HelloRequest")
    assert paths["/sessions"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/OpenSessionRequest")
    assert paths["/sessions/{session_id}/commands"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/CommandListResponse")
