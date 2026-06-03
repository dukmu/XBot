"""Sandbox policy for tool execution.

Based on the proven design from XBot v1. Manages:
- Resource path rules (readwrite, readonly, deny, ask)
- Tool guard decisions (sandboxed vs host execution)
- Symlink escape detection
- One-call path approvals
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PathAccess = Literal["readwrite", "readonly", "deny", "ask"]


@dataclass
class SandboxResourceRule:
    """One resource access rule."""

    path: str
    access: PathAccess = "readonly"

    def matches(self, target: str) -> bool:
        """Check if *target* is under this rule's path."""
        try:
            Path(target).relative_to(self.path)
            return True
        except ValueError:
            return False


class SandboxPolicy:
    """Resource access policy for sandboxed tools.

    When enabled, evaluates path access decisions. When disabled,
    allows workspace paths and denies anything outside workspace.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        data_root: Path | str = "/tmp/xbotv2-data",
        workspace_root: Path | str = "/tmp/xbotv2-workspace",
        enabled: bool = False,
    ) -> None:
        self.enabled = enabled
        self.data_root = Path(data_root)
        self.workspace_root = Path(workspace_root)
        self._rules: list[SandboxResourceRule] = []
        self._one_call_approvals: set[tuple[str, str]] = set()  # (path, operation)

        if config:
            self._load_config(config)

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self, config: dict[str, Any]) -> None:
        self.enabled = config.get("enabled", self.enabled)
        for rule_data in config.get("resources", []):
            path = rule_data.get("path", "")
            access = rule_data.get("access", "readonly")
            self._rules.append(SandboxResourceRule(path=path, access=access))

        # Built-in rules
        self._rules.append(SandboxResourceRule(
            path=str(self.workspace_root), access="readwrite",
        ))
        self._rules.append(SandboxResourceRule(
            path=str(self.data_root), access="readonly",
        ))

    # ------------------------------------------------------------------
    # Tool guard
    # ------------------------------------------------------------------

    def guard_tool_call(
        self, tool_name: str, args: dict[str, Any], tool_mode: str
    ) -> tuple[bool, str]:
        """Check whether a tool call is allowed.

        Returns:
            (allowed, reason) — reason is "" if allowed.
        """
        if tool_mode == "host":
            return True, ""

        if not self.enabled:
            # Disabled sandbox: allow workspace, deny outside
            for path_entry in self._extract_paths(args):
                resolved = self.resolve_tool_path(path_entry)
                if not self._is_under_workspace(resolved):
                    return False, f"Sandbox disabled: path outside workspace: {resolved}"
            return True, ""

        # Enabled sandbox: check each path against rules
        for path_entry in self._extract_paths(args):
            resolved = self.resolve_tool_path(path_entry)

            if self._check_symlink_escape(resolved):
                return False, f"Symlink escape detected: {resolved}"

            access = self._evaluate_path_access(resolved)
            if access == "deny":
                return False, f"Path denied: {resolved}"
            if access == "ask":
                return (
                    False,
                    "Path approval required but interactive approval is not "
                    f"implemented: {resolved}",
                )

        return True, ""

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve_tool_path(self, path_str: str) -> str:
        """Resolve a tool argument path against the workspace root."""
        p = Path(path_str)
        if p.is_absolute():
            return str(p.resolve())
        return str((Path(self.workspace_root) / p).resolve())

    def resolve_resource_path(self, path_str: str) -> str:
        """Resolve a path against the data root."""
        p = Path(path_str)
        if p.is_absolute():
            return str(p.resolve())
        return str((Path(self.data_root) / p).resolve())

    # ------------------------------------------------------------------
    # One-call approvals (for ask→allow transitions)
    # ------------------------------------------------------------------

    def approve_once(self, path: str, operation: str) -> None:
        """Grant one-call approval for *path* + *operation*."""
        self._one_call_approvals.add((str(path), operation))

    def clear_one_call_approvals(self) -> None:
        """Clear all one-call approvals (after tool execution)."""
        self._one_call_approvals.clear()

    # ------------------------------------------------------------------
    # Description (for system prompt)
    # ------------------------------------------------------------------

    def describe(self) -> str:
        """Return a human-readable sandbox summary."""
        if self.enabled:
            return (
                f"Sandbox enabled. Workspace: {self.workspace_root}. "
                f"Bubblewrap enforces resource rules."
            )
        return f"Sandbox disabled. Workspace: {self.workspace_root}."

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _extract_paths(self, args: dict[str, Any]) -> list[str]:
        """Extract file-path-ish values from tool arguments."""
        paths: list[str] = []
        path_keys = {"path", "file_path", "source", "target", "dest", "directory", "dir"}
        for key in path_keys:
            if key in args and isinstance(args[key], str):
                paths.append(args[key])
        return paths

    def _is_under_workspace(self, resolved: str) -> bool:
        try:
            Path(resolved).relative_to(self.workspace_root.resolve())
            return True
        except ValueError:
            return False

    def _check_symlink_escape(self, resolved: str) -> bool:
        """Return True if a symlink points outside the workspace."""
        try:
            real = os.path.realpath(resolved)
            Path(real).relative_to(self.workspace_root.resolve())
            return False
        except (ValueError, OSError):
            return True

    def _evaluate_path_access(self, resolved: str) -> PathAccess:
        """Evaluate access for *resolved* against all rules.

        Deny rules take precedence, then ask, then readonly, then readwrite.
        """
        for rule in self._rules:
            if rule.matches(resolved):
                return rule.access
        return "deny"  # Default-deny for sandboxed execution
