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
