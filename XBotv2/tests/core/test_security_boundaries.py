"""Permission grants cannot widen execution or filesystem boundaries."""

import logging
from pathlib import Path

import pytest
from xcore.state import StateService
from XBotv2.permissions import ApprovalDecision

from XBotv2.application.logging import setup_logging
from XBotv2.core import ToolCall
from XBotv2.core.variables import RuntimeVariables
from XBotv2.permissions.plugin import PermissionHandlers, PermissionsService
from XBotv2.permissions.rules import permission_rule_for_tool_call
from XBotv2.permissions.system import PermissionSystem
from XBotv2.permissions.tools import request_tool_permission
from XBotv2.sandbox.policy import SandboxPolicy


def test_shell_session_approval_does_not_authorize_another_command():
    call = ToolCall(id="1", name="shell", args={
        "command": "git status", "cwd": "/workspace",
        "sandbox_permissions": "require_escalated",
    })
    system = PermissionSystem({"allow": [permission_rule_for_tool_call(call)]})
    assert system.check_tool_call(call)[0] == "allow"
    different = call.model_copy(update={"args": {**call.args, "command": "git push"}})
    assert system.check_tool_call(different)[0] == "ask"


def test_once_escalation_grant_is_consumed_after_escape_check():
    system = PermissionSystem()
    system.grant_once("shell", {"command": "pwd", "sandbox_permissions": "require_escalated"})
    call = ToolCall(id="1", name="shell", args={
        "command": "pwd", "sandbox_permissions": "require_escalated",
    })
    assert system.check_tool_call(call)[0] == "allow"
    assert system.check_tool_call(call)[0] == "ask"


@pytest.mark.asyncio
async def test_implicit_shell_cwd_grant_is_workspace_scoped(tmp_path):
    service = PermissionsService({}, RuntimeVariables({"workspace": tmp_path}), StateService(path=tmp_path / "permissions.json"))
    call = ToolCall(id="cwd", name="shell", args={"command": "pwd"})
    await service.grant_session(service.rule_for_call(call))
    assert service.check_tool_call(call)[0] == "allow"
    assert service.check("shell", {"command": "pwd", "cwd": str(tmp_path)}) == "allow"
    assert service.check("shell", {"command": "pwd", "cwd": "/outside"}) == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["once", "session"])
async def test_proactive_grants_are_local_and_audited(scope, tmp_path):
    service = PermissionsService({}, RuntimeVariables(), StateService(path=tmp_path / "permissions.json"))
    notices = []

    async def emit(name, event):
        notices.append(event)

    class Approval:
        async def request(self, event):
            return ApprovalDecision(decision="allow", scope=scope)

    store_path = tmp_path / "permissions.json"
    handlers = PermissionHandlers(service, emit)
    result = await request_tool_permission(
        "shell", {"command": "pwd"}, "inspect",
        approval=Approval(), apply_permission_decision=handlers.apply_decision,
    )
    assert result.status == "success"
    assert notices[0].scope == scope
    assert notices[0].source == "request_permission"
    assert notices[0].request_id.startswith("permission:")
    assert service.check("shell", {"command": "pwd"}) == "allow"
    assert service.check_tool_call(ToolCall(id="once", name="shell", args={"command": "pwd"}))[0] == "allow"
    assert service.check("shell", {"command": "pwd"}) == ("ask" if scope == "once" else "allow")
    assert PermissionsService({}, RuntimeVariables(), StateService(path=tmp_path / "permissions.json")).check("shell", {"command": "pwd"}) == "ask"
    restored = PermissionsService({}, RuntimeVariables(), StateService(path=store_path))
    await restored.restore()
    assert restored.check("shell", {"command": "pwd"}) == ("allow" if scope == "session" else "ask")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tool", "proactive"])
@pytest.mark.parametrize("response", [
    {"decision": "allow", "scope": "global"},
    {"decision": "invalid", "scope": "once"},
])
async def test_invalid_approval_never_grants_and_is_audited(source, response, caplog, tmp_path):
    from XBotv2.permissions.guard import PermissionGuard

    service = PermissionsService({}, RuntimeVariables(), StateService(path=tmp_path / "permissions.json"))
    notices = []

    async def emit(name, event):
        notices.append(event)

    class Approval:
        async def request(self, event):
            return ApprovalDecision.model_validate(response)

    handlers = PermissionHandlers(service, emit)
    caplog.set_level(logging.INFO, logger="xbotv2.permissions")
    with pytest.raises(ValueError):
        if source == "tool":
            await PermissionGuard(service, Approval(), emit, handlers.apply_decision).check(
                ToolCall(id="invalid", name="shell", args={"command": "pwd"}), None,
            )
        else:
            await request_tool_permission(
                "shell", {"command": "pwd"}, "inspect",
                approval=Approval(), apply_permission_decision=handlers.apply_decision,
            )
    assert not any(getattr(notice, "decision", None) == "allow" for notice in notices)
    assert service.check("shell", {"command": "pwd"}) == "ask"
    assert "permission.requested" in caplog.text
    assert "permission.finished" in caplog.text


def test_permission_regex_limits_fail_closed():
    from XBotv2.permissions.patterns import (
        MAX_MATCHES,
        MAX_PATTERN_CHARS,
        MAX_VALUE_CHARS,
    )

    with pytest.raises(ValueError, match="4096"):
        PermissionSystem({"deny": [{"tool": "x" * (MAX_PATTERN_CHARS + 1)}]})
    system = PermissionSystem({"deny": [{"tool": "shell", "params": {"command": "(a|aa)+$"}}]}, default_decision="allow")
    with pytest.raises(ValueError, match="time limit"):
        system.check("shell", {"command": "a" * 10000 + "!"})
    with pytest.raises(ValueError, match="size limit"):
        system.check("shell", {"command": "a" * (MAX_VALUE_CHARS + 1)})
    oversized_policy = PermissionSystem({
        "deny": [{"tool": "never"}] * (MAX_MATCHES + 1),
    })
    with pytest.raises(ValueError, match="aggregate matching budget"):
        oversized_policy.check("shell")


def test_permission_structured_parameters_use_canonical_json():
    system = PermissionSystem({
        "allow": [{
            "tool": "structured",
            "params": {"payload": r'\{"a":1,"b":\[2,3\]\}'},
        }],
    })

    assert system.check("structured", {"payload": {"b": [2, 3], "a": 1}}) == "allow"
    assert system.check("structured", {"payload": {"a": 2, "b": [2, 3]}}) == "ask"


def test_parent_once_grant_survives_child_denial_and_preview():
    parent = PermissionSystem()
    parent.grant_once("shell", {"command": "pwd"})
    child = PermissionSystem({"deny": [{"tool": "shell"}]}, parent=parent)
    assert child.check("shell", {"command": "pwd"}) == "deny"
    assert parent.check("shell", {"command": "pwd"}) == "allow"
    call = ToolCall(id="parent", name="shell", args={"command": "pwd"})
    assert child.check_tool_call(call)[0] == "deny"
    assert parent.check_tool_call(call)[0] == "allow"
    assert parent.check_tool_call(call)[0] == "ask"


@pytest.mark.asyncio
async def test_application_restores_only_its_thread_grants(tmp_path):
    from XBotv2.application.app import start_application
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM

    class Approval:
        async def request(self, event):
            return ApprovalDecision(decision="allow", scope="session")

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    for thread, grant in (("agent", True), ("agent", False), ("other", False)):
        app = await start_application(
            paths=paths, session_id="persistent", thread_id=thread,
            workspace_root=tmp_path, llm_override=MockLLM(responses=[]),
        )
        try:
            if grant:
                handlers = PermissionHandlers(app.permissions, app.emit)
                await request_tool_permission(
                    "probe", {"value": "safe"}, "future check",
                    approval=Approval(), apply_permission_decision=handlers.apply_decision,
                )
            assert app.permissions.check("probe", {"value": "safe"}) == ("allow" if thread == "agent" else "ask")
        finally:
            await app.stop()


@pytest.mark.asyncio
async def test_permission_request_only_grants_future_calls_and_keeps_sandbox(tmp_path):
    service = PermissionsService({}, RuntimeVariables(), StateService(path=tmp_path / "permissions.json"))
    sandbox = SandboxPolicy(workspace_root=tmp_path, external_write="deny")
    before = sandbox.export_config()
    notices = []

    async def emit(name, event):
        notices.append((name, event))

    class Approval:
        async def request(self, event):
            assert event.data["permission"] == {
                "tool": "edit", "params": {"path": "/outside/report", "mode": "write"},
            }
            assert "tool_call" not in event.data
            return ApprovalDecision(decision="allow", scope="session")

    handlers = PermissionHandlers(service, emit)
    result = await request_tool_permission(
        "edit", {"path": "/outside/report", "mode": "write"}, "future reports",
        approval=Approval(), apply_permission_decision=handlers.apply_decision,
    )
    assert result.status == "success"
    args = {"path": "/outside/report", "mode": "write"}
    assert service.check("edit", args) == "allow"
    assert sandbox.check_tool_access("edit", args)[0]["decision"] == "deny"
    assert sandbox.export_config() == before
    assert [item.name for item in tmp_path.iterdir()] == ["permissions.json"]
    service.configure_agent(None)
    assert service.check("edit", args) == "allow"
    assert PermissionsService({}, RuntimeVariables(), StateService(path=tmp_path / "permissions.json")).check("edit", args) == "ask"


def test_transport_records_have_a_separate_file(tmp_path):
    path = setup_logging(data_dir=tmp_path)
    logging.getLogger("xbotv2.tools").info("tool-only")
    logging.getLogger("xbotv2.api").info("api-only")
    logging.getLogger("uvicorn.access").info("access-only")
    logging.getLogger("httpx").info("client-only")
    runtime = path.read_text()
    transport = path.with_name(path.stem + ".transport" + path.suffix).read_text()
    assert "tool-only" in runtime and "tool-only" not in transport
    for message in ("api-only", "access-only", "client-only"):
        assert message not in runtime
        assert transport.count(message) == 1


@pytest.mark.asyncio
async def test_sandbox_cannot_read_or_modify_host_tmp(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "outside"
    secret.write_text("host-private")
    sandbox = SandboxPolicy(workspace_root=workspace, data_root=tmp_path / "data")
    if not sandbox.backend_available:
        pytest.skip("bubblewrap unavailable")
    import shlex
    output = await sandbox.run_shell(
        f"test ! -e {shlex.quote(str(secret))} && printf isolated", shell="/bin/sh",
    )
    assert output == "isolated"
    assert secret.read_text() == "host-private"


@pytest.mark.asyncio
async def test_external_deny_and_policy_directory_are_os_enforced(tmp_path):
    from XBotv2.config.models import SandboxConfig, SandboxResourceConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    denied = workspace / "private.txt"
    denied.write_text("private")
    sandbox = SandboxPolicy(
        SandboxConfig(external_read="deny", resources=[
            SandboxResourceConfig(path=str(denied), access="deny"),
        ]),
        workspace_root=workspace, data_root=tmp_path / "data",
    )
    if not sandbox.backend_available:
        pytest.skip("bubblewrap unavailable")
    result = await sandbox.run_shell(
        "set -eu; test ! -s private.txt; test ! -e /var; "
        "if touch .xbot/plugins.yaml 2>/dev/null; then exit 1; fi; printf protected",
        shell="/bin/sh",
    )
    assert result == "protected"
    assert denied.read_text() == "private"
    assert not (workspace / ".xbot" / "plugins.yaml").exists()
