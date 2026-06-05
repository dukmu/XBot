"""Permission and sandbox policy overlay/persistence helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


PermissionScope = str


def load_session_policy(config_dir: Path, session_id: str) -> dict[str, Any]:
    """Load optional session-local policy overlay."""
    return _read_yaml(_session_policy_path(config_dir, session_id))


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
) -> dict[str, Any]:
    """Merge sandbox config with session resources before personality resources."""
    base = dict(base or {})
    overlay = dict(overlay or {})
    resources = list(overlay.get("resources", [])) + list(base.get("resources", []))
    merged = {**base, **overlay}
    if resources:
        merged["resources"] = resources
    return merged


def persist_permission_decision(
    *,
    config_dir: Path,
    personality_id: str,
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
    - ``always``: write personalities/<id>/personality.yaml
    """
    decision = decision.lower().strip()
    scope = (scope or "once").lower().strip()
    if decision not in {"allow", "deny"} or scope not in {"session", "always"}:
        return

    data = client_event.get("data") or {}
    source = str(data.get("source") or "permission_system")
    tool_call = data.get("tool_call") if isinstance(data.get("tool_call"), dict) else {}
    if not tool_call:
        return

    if source == "sandbox":
        _persist_sandbox_rule(
            config_dir=config_dir,
            personality_id=personality_id,
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
    path = _policy_target_path(config_dir, personality_id, session_id, scope)
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
    config_dir: Path,
    personality_id: str,
    session_id: str,
    tool_call: dict[str, Any],
    decision: str,
    scope: PermissionScope,
    engine: Any | None,
) -> None:
    paths = _tool_call_paths(tool_call, config_dir / "sessions" / session_id / "workspace")
    if not paths:
        return
    path = _policy_target_path(config_dir, personality_id, session_id, scope)
    doc = _read_yaml(path)
    sandbox = doc.setdefault("sandbox", {})
    sandbox["enabled"] = True
    resources = sandbox.setdefault("resources", [])
    access = "readwrite" if decision == "allow" else "deny"
    for resolved in paths:
        rule = {"path": resolved, "access": access}
        if rule not in resources:
            resources.insert(0, rule)
        if engine is not None and getattr(engine, "sandbox_policy", None) is not None:
            engine.sandbox_policy.add_resource_rule(resolved, access)
    _write_yaml(path, doc)


def _policy_target_path(
    config_dir: Path,
    personality_id: str,
    session_id: str,
    scope: PermissionScope,
) -> Path:
    if scope == "always":
        return config_dir / "personalities" / personality_id / "personality.yaml"
    return _session_policy_path(config_dir, session_id)


def _session_policy_path(config_dir: Path, session_id: str) -> Path:
    return config_dir / "sessions" / session_id / "policy.yaml"


def _permission_rule_for_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(tool_call.get("name") or "")
    if not tool_name:
        return {}
    rule: dict[str, Any] = {"tool": re.escape(tool_name)}
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    params = {
        key: re.escape(str(value))
        for key, value in sorted(args.items())
        if isinstance(value, (str, int, float, bool))
    }
    if params:
        rule["params"] = params
    return rule


def _tool_call_paths(tool_call: dict[str, Any], workspace_root: Path) -> list[str]:
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    path_keys = {"path", "file_path", "source", "target", "dest", "directory", "dir"}
    paths: list[str] = []
    for key in path_keys:
        value = args.get(key)
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
