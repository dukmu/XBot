"""Contract tests for the supported XBotv2 extension surface."""

import inspect
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import XBotv2.core as public_api

from XBotv2.agents import AgentDefinition, AgentSession
from XBotv2.context_builder import ContextComponent
from XBotv2.core import (
    prompt_container,
    prompt_element,
    RuntimePaths,
    RuntimeVariables,
    SessionPaths,
    ToolCall,
    ToolResult,
    Tool,
)
from XBotv2.agentloop import EventContext, Events
from XBotv2.commands import Command, CommandResult
from XBotv2.config import SessionPolicyPatch
from XBotv2.interactions import UserInputRequiredData
from XBotv2.jobs import Job, JobKind
from XBotv2.protocol import HelloRequest, server_event
from XBotv2.protocol.version import PROTOCOL_VERSION
from XBotv2.session import MessageRequest


def test_public_api_inventory_is_explicit():
    inventory = Path(__file__).parents[2] / "docs" / "api" / "api_inventory.md"
    documented = []
    for line in inventory.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Exported Symbols"):
            if line.startswith("## Exported Symbols (XBotv2.jobs)"):
                break
            continue
        if match := re.match(r"^\| `([^`]+)` \|", line):
            documented.append(match.group(1))

    assert documented == public_api.__all__
    assert len(documented) == len(set(documented))
    assert all(hasattr(public_api, name) for name in documented)


def test_plugin_package_roots_export_declarations_not_implementations():
    import XBotv2.agentloop as agentloop
    import XBotv2.agents as agents
    import XBotv2.application as application
    import XBotv2.commands as commands
    import XBotv2.compact as compact
    import XBotv2.config as config
    import XBotv2.context_builder as context_builder
    import XBotv2.jobs as jobs
    import XBotv2.llm as llm
    import XBotv2.loader as loader
    import XBotv2.mcp_plugin as mcp_plugin
    import XBotv2.permissions as permissions
    import XBotv2.sandbox as sandbox
    import XBotv2.server as server
    import XBotv2.session as session

    assert set(jobs.__all__) >= {
        "JobRunner",
        "JobsPort",
        "JobsCommandPort",
        "LIST_TASKS",
        "TaskSnapshot",
    }
    assert set(commands.__all__) >= {
        "Command",
        "CommandResult",
        "EXECUTE_COMMAND",
        "LIST_COMMANDS",
    }
    assert set(llm.__all__) >= {
        "LIST_PROVIDERS",
        "LlmCatalogPort",
        "ModelPort",
        "ProviderCatalog",
    }
    assert mcp_plugin.__all__ == ["MCP_PLUGIN_ID"]
    assert set(permissions.__all__) == {
        "PERMISSION_DECIDED",
        "PermissionDecided",
        "PermissionsPort",
        "build_permissions_commands",
    }
    assert sandbox.__all__ == ["build_sandbox_commands"]
    assert set(session.__all__) >= {
        "AgentApplicationFactory",
        "SessionsPort",
        "SessionPort",
        "SessionInfo",
        "SessionRef",
        "ThreadSummary",
    }
    assert set(server.__all__) >= {
        "ModelOverride",
        "RouteContribution",
        "ServerOptions",
    }
    assert set(agentloop.__all__) >= {
        "AgentLoopDriverPort",
        "EventContext",
        "EventPort",
        "Events",
        "LoopFactoryOptions",
        "LoopSettings",
        "LoopState",
        "SHORT_CIRCUIT_EVENTS",
        "ToolsPort",
    }
    assert set(agents.__all__) >= {
        "AgentCatalogPort",
        "AgentCreateOptions",
        "AgentDefinition",
        "AgentMode",
        "AgentRuntimePort",
        "AgentSession",
        "AgentSessionResult",
        "INITIALIZE_AGENT",
    }
    assert set(config.__all__) >= {
        "PatchPolicy",
        "POLICY_CHANGED",
        "PolicyChanged",
        "SettingsPort",
    }
    assert set(context_builder.__all__) == {
        "ContextComponent",
        "PromptFragmentStage",
    }
    assert set(loader.__all__) >= {
        "PluginEntry",
        "PluginTree",
        "RELOAD_PLUGINS",
        "ReloadPlan",
    }
    assert set(application.__all__) >= {
        "AgentApplicationPort",
        "ClientEventsPort",
        "StatusSlots",
    }
    assert set(compact.__all__) >= {
        "AfterCompact",
        "BeforeCompact",
        "CompactionMetrics",
        "CompactionStartedData",
        "POST_COMPACT",
        "PRE_COMPACT",
        "compact_event",
    }
    for module, forbidden in (
        (agentloop, {"Engine", "ToolRegistry", "ToolsService"}),
        (agents, {"AgentService", "AgentRegistry"}),
        (application, {"MountedAgentApplication", "start_application"}),
        (compact, {"CompactService"}),
        (loader, {"Loader", "LoaderComponent"}),
        (jobs, {"JobRegistry", "TextOutputStore"}),
        (llm, {"LlmService", "ModelService"}),
        (permissions, {"PermissionSystem"}),
        (sandbox, {"SandboxPolicy"}),
        (server, {"create_app", "SessionHttpAdapter"}),
        (session, {"Session", "SessionManager", "SessionRuntime"}),
    ):
        assert forbidden.isdisjoint(module.__all__)
        assert all(not hasattr(module, name) for name in forbidden)


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
    assert Command(name="sample", description="Sample", handler=lambda *_: None).name == "sample"
    assert CommandResult("done").status == "ok"
    assert prompt_container(
        "root", [prompt_element("item", "a < b")]
    ) == "<root>\n<item>\na &lt; b\n</item>\n</root>"
    assert SessionPaths is not None
    assert EventContext(
        request_id="request-1",
    ).request_id == "request-1"
    assert AgentDefinition(name="sample", description="Sample").name == "sample"
    assert inspect.isclass(AgentSession)


def test_public_job_contract_excludes_registry_runtime_state():
    job = Job("job-1", JobKind.SHELL)

    assert not hasattr(job, "completion_event")
    assert not hasattr(job, "runner_task")
    assert not hasattr(job, "runtime_handle")


def test_runtime_variables_are_read_only_and_expand_consistently(tmp_path):
    runtime = RuntimePaths.from_data_dir(tmp_path / "data")
    thread = runtime.session("session-1").thread("agent")
    variables = RuntimeVariables.for_thread(runtime, tmp_path / "workspace", thread)

    assert variables["tool_results"] == str(
        thread.artifacts_dir / "tool_results"
    )
    assert variables.expand("Read ${tool_results}/result.txt") == (
        f"Read {thread.artifacts_dir}/tool_results/result.txt"
    )
    assert variables.expand_markdown(
        "Literal ${workspace}.\n\n```var\n${workspace}\n```"
    ) == f"Literal ${{workspace}}.\n\n{tmp_path / 'workspace'}"
    assert re.fullmatch(
        variables.expand_regex("${workspace}/generated/.*"),
        str(tmp_path / "workspace" / "generated" / "result.txt"),
    )
    with pytest.raises(ValueError, match="Unknown runtime variable"):
        variables.expand("${UNKNOWN}")
    assert variables.expand_markdown("```var\n${workspace}/src\n```") == (
        "```var\n${workspace}/src\n```"
    )
    with pytest.raises(TypeError):
        variables["workspace"] = "/changed"  # type: ignore[index]


def test_tool_from_function_preserves_docstring_and_exports_json_schema():
    from typing import Literal

    def edit(
        path: str,
        mode: Literal["append", "overwrite"] = "append",
        expected_sha256: str | None = None,
    ):
        """Edit a file with one explicit mode.

        Args:
            path: Destination file path inside the workspace.
            mode: Whether to append or replace the complete file.
        """

    schema = Tool.from_function(edit).provider_schema()["function"]

    assert schema["description"] == inspect.getdoc(edit)
    assert schema["parameters"]["properties"]["path"] == {"type": "string"}
    assert schema["parameters"]["properties"]["mode"] == {
        "type": "string",
        "enum": ["append", "overwrite"],
    }
    assert schema["parameters"]["properties"]["expected_sha256"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
    assert schema["parameters"]["required"] == ["path"]
    assert schema["parameters"]["additionalProperties"] is False

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


def test_wire_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        HelloRequest.model_validate({"protocol_version": PROTOCOL_VERSION, "unknown": True})


def test_message_request_rejects_blank_content():
    with pytest.raises(ValidationError):
        MessageRequest(content="   ")


def test_message_request_accepts_image_only_content():
    request = MessageRequest(
        images=[{"data": "aW1hZ2U=", "media_type": "image/png"}]
    )
    assert request.content == ""


def test_message_request_accepts_attachment_only_content():
    request = MessageRequest(attachments=[{
        "data": "YmluYXJ5",
        "media_type": "application/octet-stream",
        "name": "sample.bin",
    }])
    assert request.content == ""


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
    assert "ok" in event.data["content"]


def test_interactions_protocol_rejects_ask_user_without_choices():
    with pytest.raises(
        ValidationError,
        match="ask_user requires at least two options",
    ):
        UserInputRequiredData(
            request_id="user_input:c1",
            source="ask_user",
            tool_call_id="c1",
            question="Continue?",
        )


@pytest.mark.asyncio
async def test_openapi_uses_typed_request_contracts(tmp_path):
    from XBotv2.application.server import start_server_application

    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "plugins.yaml").write_text(
        yaml.safe_dump([{
            "id": "llm",
            "name": "llm",
            "config": {
                "default": "test",
                "providers": {
                    "test": {
                        "protocol": "openai",
                        "api_key": "test",
                        "default_model": "test",
                        "models": [{"model": "test"}],
                    }
                },
            },
        }]),
        encoding="utf-8",
    )
    application = await start_server_application(
        paths=RuntimePaths.from_data_dir(data_dir),
        provider_name="test",
        workspace_root=str(tmp_path),
        no_plugins=True,
    )
    try:
        schema = application.server.openapi()
    finally:
        await application.stop()
    assert schema["info"]["version"] == PROTOCOL_VERSION
    paths = schema["paths"]
    assert set(paths) == {
        "/health",
        "/hello",
        "/providers",
        "/sessions",
        "/sessions/{session_id}",
        "/sessions/{session_id}/close",
        "/sessions/{session_id}/fork",
        "/sessions/{session_id}/policy",
        "/sessions/{session_id}/threads",
        "/sessions/{session_id}/threads/{thread_id}",
        "/sessions/{session_id}/threads/{thread_id}/agent",
        "/sessions/{session_id}/threads/{thread_id}/agents",
        "/sessions/{session_id}/threads/{thread_id}/agents/reload",
        "/sessions/{session_id}/threads/{thread_id}/close",
        "/sessions/{session_id}/threads/{thread_id}/config/reload",
        "/sessions/{session_id}/threads/{thread_id}/effort",
        "/sessions/{session_id}/threads/{thread_id}/events",
        "/sessions/{session_id}/threads/{thread_id}/history/clear",
        "/sessions/{session_id}/threads/{thread_id}/history/undo",
        "/sessions/{session_id}/threads/{thread_id}/interactions/permission-response",
        "/sessions/{session_id}/threads/{thread_id}/interactions/user-input",
        "/sessions/{session_id}/threads/{thread_id}/interrupt",
        "/sessions/{session_id}/threads/{thread_id}/messages",
        "/sessions/{session_id}/threads/{thread_id}/provider",
        "/sessions/{session_id}/threads/{thread_id}/tasks",
        "/sessions/{session_id}/threads/{thread_id}/tasks/stop",
        "/sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop",
        "/sessions/{session_id}/threads/{thread_id}/tools",
    }
    assert paths["/hello"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/HelloRequest")
    assert paths["/sessions"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/OpenSessionRequest")
    policy_path = "/sessions/{session_id}/policy"
    assert paths[policy_path]["patch"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/SessionPolicyPatch")
    assert paths[policy_path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/SessionPolicyResponse")
    assert "/commands" not in paths
    assert not any(path.endswith("/commands") for path in paths)
    assert paths["/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/HealthResponse")
    assert paths["/sessions"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/SessionListResponse")
    thread_path = "/sessions/{session_id}/threads/{thread_id}"
    assert paths[thread_path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/ThreadSummary")
    assert paths[thread_path]["get"]["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    undo_path = "/sessions/{session_id}/threads/{thread_id}/history/undo"
    assert paths[undo_path]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/UndoRequest")
    assert paths[undo_path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/HistoryMutationResponse")
    assert paths["/sessions"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    message_path = "/sessions/{session_id}/threads/{thread_id}/messages"
    event_path = "/sessions/{session_id}/threads/{thread_id}/events"
    assert set(paths[message_path]["post"]["responses"]["200"]["content"]) == {
        "text/event-stream"
    }
    assert set(paths[event_path]["get"]["responses"]["200"]["content"]) == {
        "text/event-stream"
    }

    operation_ids = [
        operation["operationId"]
        for methods in paths.values()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_session_policy_patch_rejects_ambiguous_or_mistyped_values():
    with pytest.raises(ValidationError):
        SessionPolicyPatch(
            permissions={"shell": "allow"},
            remove_permissions=["shell"],
        )
    with pytest.raises(ValidationError):
        SessionPolicyPatch(sandbox={"network": "false"})
    with pytest.raises(ValidationError):
        SessionPolicyPatch(sandbox={"external_write": True})
