"""Permission evaluation over canonical typed policies and grants."""

import re

import pytest

from XBotv2.core import ToolCall
from XBotv2.core.variables import RuntimeVariables
from XBotv2.permissions import PermissionPolicy, PermissionRule
from XBotv2.permissions.rules import permission_rule_for_tool_call
from XBotv2.permissions.system import PermissionSystem


def _policy(
    *rules: PermissionRule,
    default: str = "ask",
) -> PermissionPolicy:
    return PermissionPolicy(rules=rules, default_decision=default)


def _rule(tool: str, decision: str, **params: str) -> PermissionRule:
    return PermissionRule(
        tool_pattern=tool,
        param_patterns=params,
        decision=decision,
    )


def test_policy_default_is_explicit_and_deny_has_precedence():
    permissions = PermissionSystem(_policy(
        _rule(".*", "allow"),
        _rule("shell", "deny"),
        default="ask",
    ))

    assert permissions.check("read") == "allow"
    assert permissions.check("shell") == "deny"
    assert PermissionSystem(_policy(default="deny")).check("unknown") == "deny"


def test_parent_and_child_policies_form_a_restrictive_intersection():
    parent = PermissionSystem(_policy(_rule("shell", "deny"), default="allow"))
    child = PermissionSystem(
        _policy(_rule("shell", "allow"), default="allow"), parent=parent,
    )
    assert child.check("shell") == "deny"

    parent.replace_policies((_policy(_rule("shell", "allow")),))
    child.replace_policies((_policy(_rule("shell", "allow")),))
    assert child.check("shell") == "allow"


def test_parameter_rules_require_every_declared_parameter():
    permissions = PermissionSystem(_policy(
        _rule("edit", "allow", path=r"notes\.md", mode="write"),
    ))

    assert permissions.check("edit", {"path": "notes.md", "mode": "write"}) == "allow"
    assert permissions.check("edit", {"path": "notes.md"}) == "ask"
    assert permissions.check("edit", {"path": "other.md", "mode": "write"}) == "ask"


def test_once_grant_is_consumed_only_by_checking_the_matching_call():
    permissions = PermissionSystem(_policy(default="ask"))
    arguments = {"path": "report.txt", "content": "done"}
    permissions.grant_once("edit", {"path": r"report\.txt", "content": "done"})

    assert permissions.check("edit", {**arguments, "content": "other"}) == "ask"
    decision, _reason = permissions.check_tool_call(
        ToolCall(id="once", name="edit", args=arguments),
    )
    assert decision == "allow"
    assert permissions.check("edit", arguments) == "ask"


def test_deny_policy_cannot_be_overridden_by_a_grant():
    permissions = PermissionSystem(_policy(_rule("edit", "deny")))
    permissions.grant_once("edit", {"path": r"report\.txt"})
    assert permissions.check("edit", {"path": "report.txt"}) == "deny"


def test_rule_for_call_uses_tool_owned_selectors_and_typed_decision():
    call = ToolCall(id="call-1", name="edit", args={
        "path": "notes.md",
        "mode": "write",
        "content": "large private document",
    })
    rule = permission_rule_for_tool_call(
        call,
        selectors=("path", "mode"),
        decision="allow",
    )

    assert rule == PermissionRule(
        tool_pattern="edit",
        param_patterns={"mode": "write", "path": r"notes\.md"},
        decision="allow",
    )
    assert "content" not in rule.param_patterns


def test_rule_for_undeclared_tool_constrains_all_scalar_arguments():
    rule = permission_rule_for_tool_call(
        ToolCall(id="call-1", name="custom", args={
            "target": "a",
            "new_argument": "b",
            "nested": {"secret": "not a scalar"},
        }),
        decision="allow",
    )
    assert rule.param_patterns == {"new_argument": "b", "target": "a"}


def test_workspace_path_scope_checks_all_paths_and_resolves_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    permissions = PermissionSystem(
        _policy(PermissionRule(
            tool_pattern="(?:read|edit|path|search)",
            path_scope="${workspace}",
            decision="allow",
        )),
        variables=RuntimeVariables({"workspace": workspace}),
    )

    assert permissions.check("edit", {"path": "notes.md", "mode": "write"}) == "allow"
    assert permissions.check("edit", {"path": "../outside/x", "mode": "write"}) == "ask"
    assert permissions.check("edit", {"path": "link/x", "mode": "write"}) == "ask"


def test_invalid_patterns_are_rejected_when_policy_is_installed():
    with pytest.raises(ValueError, match="Invalid permission regular expression"):
        PermissionSystem(_policy(_rule("[", "allow")))
    with pytest.raises(ValueError, match="Unknown runtime variable"):
        PermissionSystem(_policy(PermissionRule(
            tool_pattern="edit",
            path_scope="${unknown}",
            decision="allow",
        )))


def test_shell_omitted_cwd_is_matched_against_runtime_workspace(tmp_path):
    workspace = str(tmp_path.resolve())
    permissions = PermissionSystem(
        _policy(_rule("shell", "allow", cwd=re.escape(workspace))),
        variables=RuntimeVariables({"workspace": workspace}),
    )
    assert permissions.check("shell", {"command": "pwd"}) == "allow"
