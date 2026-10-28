"""Permission decisions use the same resolved tree as the policy API."""

import yaml

from XBotv2.config.api.contracts import PatchPolicy
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import OpenSession, OpenThread, SESSIONS


def write_policy(path, *, rules, default="ask"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump([{
        "id": "permissions",
        "config": {"default_decision": default, "rules": [
            {"tool_pattern": tool, "decision": decision}
            for tool, decision in rules
        ]},
    }]), encoding="utf-8")


async def test_resolved_policy_and_agent_boundaries_through_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_TOKEN", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    write_policy(paths.config_dir / "plugins.yaml", rules=[
        ("new_tool", "allow"), ("blocked", "deny"), ("edit", "allow"),
    ])
    plain = tmp_path / "plain"
    workspace = tmp_path / "workspace"
    plain.mkdir()
    workspace.mkdir()
    write_policy(workspace / ".xbot" / "plugins.yaml", rules=[
        ("new_tool", "deny"), ("blocked", "allow"), ("edit", "allow"),
    ])
    write_policy(paths.session("session-policy").config_file, rules=[
        ("new_tool", "allow"), ("blocked", "deny"), ("edit", "allow"),
    ], default="deny")
    root = await open_runtime(paths=paths)
    manager = root.require(SESSIONS)

    async def open_session(sid, directory, agent=None):
        return await manager.open(OpenSession(
            sid, "main", str(directory), None, "new", False,
            selected_agent=agent, model_override=MockLLM(),
        ))

    try:
        global_scope = await open_session("global", plain)
        assert global_scope.permissions.check("new_tool") == "allow"
        assert global_scope.permissions.check("unknown") == "ask"
        assert global_scope.permissions.check("blocked") == "deny"
        global_scope.permissions.grant_once("blocked", {})
        assert global_scope.permissions.check("blocked") == "deny"

        workspace_scope = await open_session("workspace", workspace)
        assert workspace_scope.permissions.check("new_tool") == "deny"
        assert workspace_scope.permissions.check("blocked") == "allow"

        session_scope = await open_session("session-policy", workspace)
        assert session_scope.permissions.check("new_tool") == "allow"
        assert session_scope.permissions.check("blocked") == "deny"
        assert session_scope.permissions.check("unknown") == "deny"
        policy = session_scope.settings.policy().effective_permissions
        assert policy["default_decision"] == "deny"
        assert len(session_scope.settings.permission_policies()) == 1

        await session_scope.settings.update_policy(PatchPolicy(permissions={"new_tool": "ask"}))
        assert session_scope.permissions.check("new_tool") == "ask"
        session_scope.permissions.grant_once("new_tool", {})
        assert session_scope.permissions.check("new_tool") == "allow"
        session_scope.permissions.consume_once("new_tool", {})
        assert session_scope.permissions.check("new_tool") == "ask"
        assert session_scope.settings.policy().effective_permissions["default_decision"] == "deny"

        parent = await open_session("parent", plain, "Explorer")
        assert parent.permissions.check("edit") == "deny"
        child = await manager.open_thread(OpenThread(
            "parent", "child", "main", str(plain), None, "new", False,
            selected_agent="default", model_override=MockLLM(),
        ))
        child.permissions.grant_once("edit", {})
        assert child.permissions.check("edit") == "deny"
    finally:
        await root.close()


async def test_session_policy_updates_each_live_thread_with_its_own_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_TOKEN", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_policy(paths.session("shared").config_file, rules=[
        ("tool_$${thread_id}", "deny"), ("edit", "allow"), ("target", "ask"),
    ], default="allow")
    root = await open_runtime(paths=paths)
    manager = root.require(SESSIONS)
    try:
        main = await manager.open(OpenSession(
            "shared", "main", str(workspace), None, "new", False,
            model_override=MockLLM(),
        ))

        async def child(name, agent="default"):
            return await manager.open_thread(OpenThread(
                "shared", name, "main", str(workspace), None, "new", False,
                selected_agent=agent, model_override=MockLLM(),
            ))

        first = await child("first")
        second = await child("second", "Explorer")
        other = await manager.open(OpenSession(
            "other", "main", str(workspace), None, "new", False,
            model_override=MockLLM(),
        ))
        assert first.permissions.check("tool_first") == "deny"
        assert first.permissions.check("tool_second") == "allow"
        assert second.permissions.check("tool_second") == "deny"
        assert second.permissions.check("edit") == "deny"
        assert first.permissions.check("tool_main") == "deny"
        first.permissions.grant_once("tool_main", {})
        assert first.permissions.check("tool_main") == "deny"

        await first.settings.update_policy(PatchPolicy(
            permissions={"target": "deny"},
            sandbox={"external_read": "deny", "resources": [{
                "path": "/tmp/$${thread_id}", "access": "readonly",
            }]},
        ))
        for scope, thread in ((main, "main"), (first, "first"), (second, "second")):
            assert scope.permissions.check("target") == "deny"
            assert scope.settings.policy().effective_permissions["rules"][1]["tool_pattern"] == f"tool_{thread}"
            sandbox = scope.sandbox.export_config()
            assert sandbox["external_read"] == "deny"
            assert {resource["path"] for resource in sandbox["resources"]} == {f"/tmp/{thread}"}
        assert first.permissions.check("tool_first") == "deny"
        assert second.permissions.check("tool_second") == "deny"
        assert second.permissions.check("edit") == "deny"
        assert other.sandbox.external_read == "readonly"
        assert other.permissions.check("target") != "deny"
        later = await child("later")
        assert later.permissions.check("target") == "deny"
        assert later.permissions.check("tool_later") == "deny"
        assert later.sandbox.external_read == "deny"
        assert later.sandbox.export_config()["resources"][0]["path"] == "/tmp/later"
    finally:
        await root.close()
