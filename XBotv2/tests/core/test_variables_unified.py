"""The one expansion engine: two passes, one syntax, auto-expanded configs."""

import pytest

from XBotv2.config.loader import load_plugin_tree
from XBotv2.core.paths import RuntimePaths
from XBotv2.permissions.contracts import PermissionPolicy
from XBotv2.permissions.system import PermissionSystem
from XBotv2.core.variables import RuntimeVariables, expand_env_refs


def _tree(tmp_path, doc: str):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "plugins.yaml").write_text(doc, encoding="utf-8")
    return RuntimePaths.from_data_dir(tmp_path)


def test_expand_env_refs_is_strict_and_inline(monkeypatch):
    monkeypatch.setenv("XBOT_SESSION_HEADER", "opencode-session")
    assert expand_env_refs(
        "prompt/${env:XBOT_SESSION_HEADER}/end"
    ) == "prompt/opencode-session/end"
    with pytest.raises(ValueError, match="UNSET_VAR"):
        expand_env_refs("${env:UNSET_VAR}")
    # Runtime references are not env references: they survive pass one.
    assert expand_env_refs("${session_id}") == "${session_id}"


def test_runtime_variables_keep_identities_verbatim():
    variables = RuntimeVariables({
        **RuntimeVariables.from_roots(
            workspace="/w", data_dir="/d", session_dir="/s",
            thread_dir="/t", state_dir="/st",
        ),
        "session_id": "s1",
        "thread_id": "agent",
    })
    assert variables["session_id"] == "s1"
    assert variables["thread_id"] == "agent"
    assert variables["workspace"] == "/w"


def test_expand_config_recurses_and_rejects_env_at_runtime(tmp_path):
    variables = RuntimeVariables({
        **RuntimeVariables.from_roots(
            workspace="/w", data_dir="/d", session_dir="/s",
            thread_dir="/t", state_dir="/st",
        ),
        "session_id": "s1",
    })
    expanded = variables.expand_config({
        "headers": {"x-opencode-session": "${session_id}"},
        "nested": {"paths": ["${workspace}/a", "/plain"]},
        "count": 3,
    })
    assert expanded == {
        "headers": {"x-opencode-session": "s1"},
        "nested": {"paths": ["/w/a", "/plain"]},
        "count": 3,
    }
    with pytest.raises(ValueError, match="env reference"):
        variables.expand_config("${env:ANY}")


def test_two_passes_expand_provider_headers_at_session_load(tmp_path, monkeypatch):
    """Pass one (env) then pass two (runtime) expand provider config once."""
    monkeypatch.setenv("XBOT_PROVIDER_TOKEN", "tok")
    doc = """
plugins:
  - id: llm
    name: llm
    config:
      default_provider: opencode
      providers:
        opencode:
          protocol: openai
          base_url: http://127.0.0.1:11435/v1
          api_key: "${env:XBOT_PROVIDER_TOKEN}"
          default_model: gpt-4
          models:
            - model: gpt-4
          headers:
            x-opencode-session: "${session_id}"
"""
    paths = _tree(tmp_path, doc)
    tree = load_plugin_tree(paths, tmp_path, session_id="s1", thread_id="agent")

    entry = next(e for e in tree.entries if e.id == "llm")
    providers = entry.config["providers"]
    assert providers["opencode"]["api_key"] == "tok"
    assert providers["opencode"]["headers"] == {
        "x-opencode-session": "s1",
    }


def test_permission_path_scope_resolves_against_runtime_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    policy = PermissionPolicy.model_validate({
        "default_decision": "ask",
        "rules": [{
            "tool_pattern": "edit",
            "path_scope": "${workspace}",
            "decision": "allow",
        }],
    })
    permissions = PermissionSystem(
        policy,
        variables=RuntimeVariables({"workspace": str(workspace)}),
    )

    assert permissions.check("edit", {"path": "report.md", "mode": "write"}) == "allow"
    assert permissions.check("edit", {"path": "../outside.md", "mode": "write"}) == "ask"
