"""Permission and sandbox policy overlay/persistence helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from XBotv2.core.paths import RuntimePaths
from XBotv2.config.models import config_dict


PermissionScope = str
_PERMISSION_DECISIONS = ("deny", "allow", "ask")


def load_session_policy(paths: RuntimePaths, session_id: str) -> dict[str, Any]:
    """Load optional session-local policy overlay."""
    return _read_yaml(paths.session(session_id).config_file)


def patch_session_policy(
    *,
    paths: RuntimePaths,
    session_id: str,
    permissions: dict[str, str] | None = None,
    remove_permissions: Iterable[str] = (),
    sandbox: dict[str, Any] | None = None,
    remove_sandbox: Iterable[str] = (),
) -> dict[str, Any]:
    """Apply one session policy patch while preserving unrelated rules."""
    path = paths.session(session_id).config_file
    doc = _read_yaml(path)
    permission_config = doc.setdefault("permissions", {})
    for tool in (*remove_permissions, *(permissions or {})):
        _remove_rule(permission_config, {"tool": re.escape(tool)})
    for tool, decision in (permissions or {}).items():
        permission_config.setdefault(decision, []).insert(
            0, {"tool": re.escape(tool)}
        )
    if not permission_config:
        doc.pop("permissions", None)

    sandbox_config = doc.setdefault("sandbox", {})
    for key in remove_sandbox:
        sandbox_config.pop(key, None)
    sandbox_config.update(sandbox or {})
    if not sandbox_config:
        doc.pop("sandbox", None)

    if doc:
        _write_yaml(path, doc)
    elif path.exists():
        path.unlink()
    return doc


def merge_permission_config(
    base: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge permission rules, preserving deny/allow/ask precedence in PermissionSystem."""
    merged: dict[str, Any] = {
        key: list(config_dict(base).get(key, []))
        for key in _PERMISSION_DECISIONS
    }
    if overlay:
        overlay = config_dict(overlay)
        for key in _PERMISSION_DECISIONS:
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

    base = config_dict(base)
    overlay = config_dict(overlay)
    overrides = dict(overrides or {})
    resources = list(overlay.get("resources", [])) + list(base.get("resources", []))
    merged = {**base, **overlay, **overrides}
    if resources:
        merged["resources"] = resources
    return merged


def persist_permission_rule(
    *,
    paths: RuntimePaths,
    session_id: str,
    rule: dict[str, Any],
    decision: str,
    scope: PermissionScope,
) -> None:
    """Persist one already-resolved permission rule for this session."""
    decision = decision.lower().strip()
    scope = (scope or "once").lower().strip()
    if decision not in {"allow", "deny"} or scope != "session" or not rule:
        return
    _persist_permission_rule(
        paths=paths,
        session_id=session_id,
        rule=rule,
        decision=decision,
    )


def _persist_permission_rule(
    *,
    paths: RuntimePaths,
    session_id: str,
    rule: dict[str, Any],
    decision: str,
) -> None:
    path = paths.session(session_id).config_file
    doc = _read_yaml(path)
    permissions = doc.setdefault("permissions", {})
    _remove_rule(permissions, rule)
    permissions.setdefault(decision, [])
    if rule not in permissions[decision]:
        permissions[decision].insert(0, rule)
    _write_yaml(path, doc)
def _remove_rule(permissions: dict[str, Any], rule: dict[str, Any]) -> None:
    for key in _PERMISSION_DECISIONS:
        permissions[key] = [item for item in permissions.get(key, []) if item != rule]
        if not permissions[key]:
            permissions.pop(key, None)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
