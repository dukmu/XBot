"""Sandbox policy with BubblewrapBackend for tool execution.

BubblewrapBackend controls the total lifecycle of sandboxed tool calls:
mount setup, process spawn, communication, timeout, and result formatting.
Path access is enforced at the OS level through mount specifications, not
through Python-level path extraction and checking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from xbotv2.api.variables import RuntimeVariables
from xbotv2.tools import filesystem_ops
from xbotv2.tools.sandbox_bwrap import BubblewrapBackend, SandboxMountSpec, backend_available

PathAccess = Literal["allow", "readwrite", "readonly", "deny", "ask"]


@dataclass
class SandboxResourceRule:
    path: str
    access: PathAccess = "readonly"


class SandboxPolicy:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        data_root: Path | str = "/tmp/xbotv2-data",
        workspace_root: Path | str = "/tmp/xbotv2-workspace",
        session_root: Path | str | None = None,
        enabled: bool = True,
        network: bool = True,
        external_read: str = "readonly",
        external_write: str = "deny",
        workspace_read: str = "allow",
        workspace_write: str = "allow",
        variables: RuntimeVariables | None = None,
    ) -> None:
        if hasattr(config, "model_dump"):
            config = config.model_dump()
        self.enabled = enabled
        self.data_root = Path(data_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.session_root = (
            Path(session_root).resolve() if session_root is not None else None
        )
        self._network = network
        self.external_read = external_read
        self.external_write = external_write
        self.workspace_read = workspace_read
        self.workspace_write = workspace_write
        self.variables = variables or RuntimeVariables.from_roots(
            workspace=self.workspace_root,
            data_dir=self.data_root,
            session_dir=self.session_root,
        )
        self._rules: list[SandboxResourceRule] = []

        if config:
            self._load_config(config)
        self._backend = BubblewrapBackend(self.workspace_root, network=self._network)

    @property
    def network(self) -> bool:
        return self._network

    @property
    def backend_available(self) -> bool:
        return backend_available()

    def add_rule(self, path: str, access: PathAccess) -> None:
        self._rules.insert(0, SandboxResourceRule(path=path, access=access))

    def replace_config(self, config: dict[str, Any]) -> None:
        """Replace policy state without invalidating runtime references."""
        replacement = SandboxPolicy(
            config,
            data_root=self.data_root,
            workspace_root=self.workspace_root,
            session_root=self.session_root,
            variables=self.variables,
        )
        self.enabled = replacement.enabled
        self._network = replacement._network
        self.external_read = replacement.external_read
        self.external_write = replacement.external_write
        self.workspace_read = replacement.workspace_read
        self.workspace_write = replacement.workspace_write
        self._rules = replacement._rules
        self._backend = replacement._backend

    # ------------------------------------------------------------------
    # Sandbox capabilities (system I/O isolated via bwrap)
    # ------------------------------------------------------------------

    async def run_shell(
        self,
        command: str,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        spec = self._mount_specs()
        return await self._backend.run(
            ["/bin/sh", "-lc", command],
            spec,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    async def filesystem(self, operation: str, args: dict[str, Any]) -> str:
        resolved = self.resolve_filesystem_args(operation, args)
        request = json.dumps(
            {"operation": operation, "args": resolved}, ensure_ascii=False
        )
        return await self._backend.run(
            ["python3", str(Path(filesystem_ops.__file__).resolve())],
            self._filesystem_mount_specs(operation, resolved),
            cwd="/",
            stdin=request,
        )

    # ------------------------------------------------------------------
    # Mount spec assembly
    # ------------------------------------------------------------------

    def _mount_specs(self) -> list[SandboxMountSpec]:
        mounts: list[SandboxMountSpec] = []

        workspace_access = self._configured_mount_access(
            self.workspace_read,
            self.workspace_write,
        )
        if workspace_access is not None:
            mounts.append(SandboxMountSpec(
                source=self.workspace_root,
                target=self.workspace_root,
                access=workspace_access,
                kind="dir",
            ))
        if self.session_root is not None:
            mounts.append(SandboxMountSpec(
                source=self.session_root,
                target=self.session_root,
                access="readonly",
                kind="dir",
            ))

        for rule in self._rules:
            resolved = self.resolve_resource_path(rule.path)
            if (
                self.session_root is not None
                and Path(resolved).is_relative_to(self.session_root)
            ):
                continue
            kind = _path_kind(resolved)
            if rule.access == "readwrite":
                mounts.append(SandboxMountSpec(Path(resolved), Path(resolved), "readwrite", kind))
            elif rule.access == "readonly":
                mounts.append(SandboxMountSpec(Path(resolved), Path(resolved), "readonly", kind))

        worker = Path(filesystem_ops.__file__).resolve()
        if not worker.is_relative_to(self.workspace_root):
            mounts.append(SandboxMountSpec(worker, worker, "readonly", "file"))

        return mounts

    def _filesystem_mount_specs(
        self,
        operation: str,
        args: dict[str, Any],
    ) -> list[SandboxMountSpec]:
        """Add per-call mounts for approved paths outside the workspace.

        Mutations mount a parent directory because atomic replace, rename, and
        delete operate on directory entries. These mounts exist only for the
        trusted filesystem worker and do not expand shell access.
        """
        mounts = self._mount_specs()
        for field, access in filesystem_ops.PATH_ACCESS.get(operation, ()):
            value = args.get(field)
            if not isinstance(value, str):
                continue
            target = _absolute_path(Path(value))
            write = access == "write"
            if self._path_decision(target, write=write) != "allow":
                continue
            if target.is_relative_to(self.workspace_root) or (
                self.session_root is not None
                and target.is_relative_to(self.session_root)
            ):
                continue
            parent = _nearest_existing_parent(target)
            if parent == Path("/"):
                continue
            if write:
                resolved_target = target.resolve()
                mounts = [
                    mount for mount in mounts
                    if mount.target not in {target, resolved_target}
                ]
            required_access = "readwrite" if write else "readonly"
            if not _mount_covers(mounts, parent, write=write):
                mounts.append(SandboxMountSpec(
                    source=parent,
                    target=parent,
                    access=required_access,
                    kind="dir",
                ))
        return mounts

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def resolve_resource_path(self, path: str) -> str:
        p = Path(self.variables.expand(path, source="sandbox resource path"))
        return str(p.resolve() if p.is_absolute() else (self.data_root / p).resolve())

    def resolve_read_path(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return _absolute_path(p)
        if p.parts and p.parts[0] == "session" and self.session_root is not None:
            resolved = _absolute_path(self.session_root / Path(*p.parts[1:]))
            if resolved.is_relative_to(self.session_root):
                return resolved
        return _absolute_path(self.workspace_root / p)

    def resolve_write_path(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return _absolute_path(p)
        if p.parts and p.parts[0] == "session" and self.session_root is not None:
            return _absolute_path(self.session_root / Path(*p.parts[1:]))
        return _absolute_path(self.workspace_root / p)

    def resolve_filesystem_args(
        self,
        operation: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        resolved = dict(args)
        for field, access in filesystem_ops.PATH_ACCESS.get(operation, ()):
            value = args.get(field)
            if not isinstance(value, str):
                continue
            write = access == "write"
            path = self.resolve_write_path(value) if write else self.resolve_read_path(value)
            resolved[field] = str(path)
        return resolved

    def check_filesystem_access(
        self,
        operation: str,
        args: dict[str, Any],
    ) -> list[dict[str, Any]]:
        resolved = self.resolve_filesystem_args(operation, args)
        decisions = []
        for field, access in filesystem_ops.PATH_ACCESS.get(operation, ()):
            value = resolved.get(field)
            if not isinstance(value, str):
                continue
            write = access == "write"
            decision = self._path_decision(Path(value), write=write)
            if decision != "allow":
                decisions.append({
                    "field": field,
                    "path": value,
                    "write": write,
                    "decision": decision,
                })
        return decisions

    def check_tool_access(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> list[dict[str, Any]]:
        operation = filesystem_ops.TOOL_OPERATIONS.get(tool_name)
        return self.check_filesystem_access(operation, args) if operation else []

    def _path_decision(self, path: Path, *, write: bool) -> str:
        lexical = _absolute_path(path)
        if (
            write
            and self.session_root is not None
            and lexical.is_relative_to(self.session_root)
        ):
            return "deny"
        target = path.resolve()
        if self.session_root is not None and target.is_relative_to(self.session_root):
            return "deny" if write else "allow"
        for rule in self._rules:
            rule_path = Path(self.resolve_resource_path(rule.path))
            if target.is_relative_to(rule_path):
                return _access_decision(rule.access, write=write)
        if target.is_relative_to(self.workspace_root):
            configured = self.workspace_write if write else self.workspace_read
        else:
            configured = self.external_write if write else self.external_read
        return _access_decision(configured, write=write)

    @staticmethod
    def _configured_mount_access(
        read_access: str,
        write_access: str,
    ) -> Literal["readonly", "readwrite"] | None:
        if _access_decision(write_access, write=True) == "allow":
            return "readwrite"
        if _access_decision(read_access, write=False) == "allow":
            return "readonly"
        return None

    def describe(self) -> str:
        if self.enabled:
            available = "available" if self.backend_available else "unavailable"
            return (
                f"Sandbox enabled (bwrap: {available}). Workspace: {self.workspace_root}. "
                f"All file I/O and shell commands run inside bubblewrap."
            )
        return f"Sandbox disabled. Workspace: {self.workspace_root}."

    # ------------------------------------------------------------------
    # Config loading / serialisation
    # ------------------------------------------------------------------

    def _load_config(self, config: dict[str, Any]) -> None:
        self.enabled = config.get("enabled", self.enabled)
        self._network = config.get("network", True)
        self.external_read = str(config.get("external_read", "readonly"))
        self.external_write = str(config.get("external_write", "deny"))
        self.workspace_read = str(config.get("workspace_read", "allow"))
        self.workspace_write = str(config.get("workspace_write", "allow"))
        for rule_data in config.get("resources", []):
            path = self.variables.expand(
                str(rule_data.get("path", "")),
                source="sandbox resource path",
            )
            access = rule_data.get("access", "readonly")
            self._rules.append(SandboxResourceRule(path=path, access=access))

    def update_from_config(self, config: dict[str, Any]) -> None:
        """Apply a sparse config dict on top of live state.

        Keys not present in *config* are left untouched,
        but ``resources`` / ``network`` / ``enabled`` are
        reapplied fully — the existing rule-list is rebuilt.

        This is the sibling of ``_load_config`` for
        post-bootstrap live updates (e.g. ``/sandbox set``
        and session-policy reload).
        """

        if "enabled" in config:
            self.enabled = config["enabled"]
        if "network" in config:
            self._network = config["network"]
            self._backend = BubblewrapBackend(self.workspace_root, network=self._network)
        for field in ("external_read", "external_write", "workspace_read", "workspace_write"):
            if field in config:
                setattr(self, field, str(config[field]))
        if "resources" in config:
            self._rules = []
            for rule_data in config["resources"]:
                path = self.variables.expand(
                    str(rule_data.get("path", "")),
                    source="sandbox resource path",
                )
                self._rules.append(SandboxResourceRule(
                    path=path,
                    access=rule_data.get("access", "readonly"),
                ))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the live sandbox config back to the format
        used by global, session, and workspace configuration."""
        d: dict[str, Any] = {
            "enabled": self.enabled,
            "network": self._network,
            "external_read": self.external_read,
            "external_write": self.external_write,
            "workspace_read": self.workspace_read,
            "workspace_write": self.workspace_write,
        }
        rules = [
            {"path": r.path, "access": r.access}
            for r in self._rules
            if r.path != str(self.workspace_root)
            and (
                self.session_root is None
                or r.path != str(self.session_root)
            )
        ]
        if rules:
            d["resources"] = rules
        return d

    def save(self, path: Path | str) -> None:
        """Persist the live sandbox config to *path* as YAML.

        The file is written atomically: a temporary sibling is
        created, populated, and then renamed over *path*.
        """

        import tempfile

        import yaml

        target = Path(path)
        content = yaml.safe_dump(
            self.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".sandbox-", suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, str(target))


def _path_kind(path_str: str) -> Literal["file", "dir"]:
    p = Path(path_str)
    if p.exists() and p.is_file():
        return "file"
    return "dir"


def _nearest_existing_parent(path: Path) -> Path:
    parent = path.parent
    while parent != parent.parent and not parent.exists():
        parent = parent.parent
    return parent


def _mount_covers(
    mounts: list[SandboxMountSpec],
    path: Path,
    *,
    write: bool,
) -> bool:
    return any(
        (mount.kind == "dir" and path.is_relative_to(mount.target))
        and (not write or mount.access == "readwrite")
        for mount in mounts
    )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _access_decision(access: str, *, write: bool) -> str:
    if access in {"allow", "readwrite"}:
        return "allow"
    if not write and access == "readonly":
        return "allow"
    return "ask" if access == "ask" else "deny"
