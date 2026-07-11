"""Permission and sandbox policy overlay/persistence helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from xbotv2.api.paths import RuntimePaths
from xbotv2.api.tools import ToolCall


PermissionScope = str


def load_session_policy(paths: RuntimePaths, session_id: str) -> dict[str, Any]:
    """Load optional session-local policy overlay."""
    return _read_yaml(paths.session(session_id).policy_file)


def merge_permission_config(
    base: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge permission rules, preserving deny/allow/ask precedence in PermissionSystem."""
    merged: dict[str, Any] = {key: list((base or {}).get(key, [])) for key in ("deny", "allow", "ask")}
    if overlay:
        for key in ("deny", "allow", "ask"):
            merged[key] = list(overlay.get(key, [])) + merged[key]
    return {key: value for key, value in merged.items() if value}


def merge_sandbox_config(
    base: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge sandbox config: base → session overlay → live overrides.

    Session resources are prepended before global resources so
    per-session approvals take priority over the baseline config.
    """

    base = dict(base or {})
    overlay = dict(overlay or {})
    overrides = dict(overrides or {})
    resources = list(overlay.get("resources", [])) + list(base.get("resources", []))
    merged = {**base, **overlay, **overrides}
    if resources:
        merged["resources"] = resources
    return merged


def persist_sandbox_config(
    *,
    paths: RuntimePaths,
    session_id: str,
    sandbox: dict[str, Any],
) -> None:
    """Write top-level sandbox keys to the session policy file.

    Callers should strip implicit workspace/data-root rules before
    passing the dict — only user-visible keys (enabled, network,
    resources, external_read/write, …) should be persisted.
    """

    if not sandbox:
        return
    path = paths.session(session_id).policy_file
    doc = _read_yaml(path)
    sandbox_section = doc.setdefault("sandbox", {})
    clean: dict[str, Any] = {k: v for k, v in sandbox.items() if not k.startswith("_")}
    sandbox_section.update(clean)
    _write_yaml(path, doc)


def clear_sandbox_config(
    *,
    paths: RuntimePaths,
    session_id: str,
) -> None:
    """Remove the session sandbox overlay entirely from policy.yaml."""
    path = paths.session(session_id).policy_file
    doc = _read_yaml(path)
    doc.pop("sandbox", None)
    if doc:
        _write_yaml(path, doc)
    elif path.exists():
        path.unlink()


def persist_permission_decision(
    *,
    paths: RuntimePaths,
    session_id: str,
    client_event: dict[str, Any],
    decision: str,
    scope: PermissionScope,
    engine: Any | None = None,
) -> None:
    """Persist a live approval/denial when scope requests it.

    ``scope`` is one of:
    - ``once``: do not persist
    - ``session``: write sessions/<session>/policy.yaml
    """
    decision = decision.lower().strip()
    scope = (scope or "once").lower().strip()
    if decision not in {"allow", "deny"} or scope != "session":
        return

    data = client_event.get("data") or {}
    source = str(data.get("source") or "permission_system")
    raw_tool_call = data.get("tool_call")
    if not isinstance(raw_tool_call, dict):
        return
    tool_call = ToolCall.from_dict(raw_tool_call)

    if source == "sandbox":
        _persist_sandbox_rule(
            paths=paths,
            session_id=session_id,
            tool_call=tool_call,
            decision=decision,
            scope=scope,
            engine=engine,
        )
        return

    rule = _permission_rule_for_tool_call(tool_call)
    if not rule:
        return
    path = paths.session(session_id).policy_file
    doc = _read_yaml(path)
    permissions = doc.setdefault("permissions", {})
    _remove_rule(permissions, rule)
    permissions.setdefault(decision, [])
    if rule not in permissions[decision]:
        permissions[decision].insert(0, rule)
    _write_yaml(path, doc)
    if engine is not None and getattr(engine, "permission_system", None) is not None:
        engine.permission_system.add_rule(decision, rule)


def _persist_sandbox_rule(
    *,
    paths: RuntimePaths,
    session_id: str,
    tool_call: ToolCall,
    decision: str,
    scope: PermissionScope,
    engine: Any | None,
) -> None:
    workspace_root = getattr(engine, "workspace_root", paths.data_dir)
    resolved_paths = _tool_call_paths(tool_call, Path(workspace_root))
    if not resolved_paths:
        return
    path = paths.session(session_id).policy_file
    doc = _read_yaml(path)
    sandbox = doc.setdefault("sandbox", {})
    sandbox["enabled"] = True
    resources = sandbox.setdefault("resources", [])
    access = "readwrite" if decision == "allow" else "deny"
    for resolved in resolved_paths:
        rule = {"path": resolved, "access": access}
        if rule not in resources:
            resources.insert(0, rule)
        if engine is not None and getattr(engine, "sandbox_policy", None) is not None:
            engine.sandbox_policy.add_rule(resolved, access)
    _write_yaml(path, doc)


def _permission_rule_for_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    tool_name = tool_call.name
    if not tool_name:
        return {}
    rule: dict[str, Any] = {"tool": re.escape(tool_name)}
    params = {
        key: re.escape(str(value))
        for key, value in sorted(tool_call.args.items())
        if isinstance(value, (str, int, float, bool))
    }
    if params:
        rule["params"] = params
    return rule


def _tool_call_paths(tool_call: ToolCall, workspace_root: Path) -> list[str]:
    path_keys = {"path", "file_path", "source", "target", "dest", "directory", "dir"}
    paths: list[str] = []
    for key in path_keys:
        value = tool_call.args.get(key)
        if not isinstance(value, str):
            continue
        path = Path(value)
        resolved = path.resolve() if path.is_absolute() else (workspace_root / path).resolve()
        paths.append(str(resolved))
    return paths


def _remove_rule(permissions: dict[str, Any], rule: dict[str, Any]) -> None:
    for key in ("deny", "allow", "ask"):
        permissions[key] = [item for item in permissions.get(key, []) if item != rule]
        if not permissions[key]:
            permissions.pop(key, None)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
